"""Should I actually make this move, and who goes?

An alert that says "pick up Braelon Allen" has done half a job. The half it
skipped is the one that takes effort at 8am on a Sunday: working out whether
he is worth a roster spot, and which of your fifteen players is the one to
cut. Both are computable from things the app already knows, so it should not
be leaving them to you.

Three questions, in the order they actually matter:

1. **Do you even need to drop anyone?** If the roster has a hole, the whole
   comparison is moot and the answer is just yes. Nothing was checking this.
2. **Who is genuinely spare?** Anyone in your best lineup — this week's or the
   next few weeks' — is not a drop candidate, which is what stops it proposing
   you cut a player it is simultaneously telling you to start.
3. **Is the swap actually positive?** The incoming player's value over the
   horizon against the outgoing player's. Reported as a net, so a move that
   costs you points says so instead of hiding behind an exciting headline.

A speculative add is judged the same way but labelled honestly: the case for a
backup who just inherited a job is his role, not his projection, and the cost
of making that bet is stated rather than smoothed over.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import replace

from ffm.data.context import ContextEngine
from ffm.engine import lineup as lineup_mod
from ffm.models import LeagueSettings, LeagueWeek, Player

# Net points per week that make a swap worth doing.
CLEAR = 2.5
WORTH_IT = 1.0

# A vacated role this size is a job worth having rather than a lottery ticket.
# The distinction matters because a depth-chart promotion always looks weak on
# the numbers — the heir's projection was drawn while he was a backup — so
# judging it on net points alone would suppress the single most valuable alert
# the app produces. What separates the two is not the heir's projection but the
# size of the job that came free, and that is measured, not guessed.
REAL_JOB = 8.0

# Slots that do not consume an active roster spot.
_NON_ACTIVE = ("IR", "TAXI")

# Positions you can refill off waivers any week, so carrying a spare is not
# insurance, it is a wasted roster spot. The draft engine reaches the same
# conclusion for a backup QB in a single-QB league.
_STREAMABLE = ("K", "DEF")


def depth_floor(settings: LeagueSettings, position: str) -> int:
    """How many of a position you must keep to survive one absence.

    Ranking drop candidates on points alone is what produced the advice to cut
    a manager's only backup running back: he was the lowest-scoring player who
    was not starting, which is exactly what a backup is. Points do not measure
    what he is for. He is there for the week his starter is out or on bye, and
    losing him means fielding an illegal or empty lineup that week — a cost no
    projection captures.
    """
    dedicated = settings.slot_counts().get(position, 0)
    if position in _STREAMABLE:
        return dedicated
    if position == "QB" and not settings.is_superflex:
        return dedicated  # streamable too: a second QB never plays
    return dedicated + 1  # one body for an injury or a bye


@dataclass
class Move:
    """A concrete recommendation: add this, drop that, and whether to bother."""

    add: Player
    add_value: float          # adjusted points per week over the horizon
    drop: Player | None
    drop_value: float
    open_spots: int
    lineup_gain: float        # improvement to your best lineup, per week
    verdict: str  # add | worth_it | inherit | marginal | speculative | skip | hold
    speculative: bool = False
    # Set when positional depth overrode the cheapest cut.
    kept_for_depth: str = ""
    # The player whose absence created this opening, and what he was worth.
    # Set for a depth-chart promotion, where the incoming player's own
    # projection is stale by construction.
    vacated_name: str = ""
    vacated_value: float = 0.0
    # He would not start, at a position where a backup never plays (a second
    # QB outside superflex, a spare kicker or defense). His points are real;
    # they just never reach your lineup.
    never_plays: bool = False
    lines: list[str] = field(default_factory=list)

    @property
    def value_to_you(self) -> float:
        """His points that would actually reach your lineup, per week."""
        return 0.0 if self.never_plays else self.add_value

    @property
    def net(self) -> float:
        """What the whole move is worth per week, drop included.

        Measured on what reaches your lineup, not on raw projections. Netting
        projections alone priced Malik Willis, a backup behind a 20-point
        Trevor Lawrence, at "+11.1, Add him" against a bench receiver.
        """
        if self.open_spots > 0 or self.drop is None:
            return round(self.value_to_you, 2)
        return round(self.value_to_you - self.drop_value, 2)

    @property
    def do_it(self) -> bool:
        return self.verdict in ("add", "worth_it", "inherit")

    @property
    def actionable(self) -> bool:
        """Worth interrupting someone for.

        A flyer that costs points is not a recommendation, it is a suggestion
        to gamble, and it does not belong in a push notification. Neither is
        swapping one bench player for a slightly better one: it changes no
        lineup, and a +1.1 on two backups is inside the noise. A phone buzzes
        for a move that starts, a job that came free, or a spot left empty.
        Everything else still appears on the waiver board, which you read when
        you choose to.
        """
        if not self.do_it:
            return False
        return (self.lineup_gain >= WORTH_IT or self.verdict == "inherit"
                or self.open_spots > 0)

    @property
    def summary(self) -> str:
        return {
            "add": "Add him",
            "worth_it": "Worth doing",
            "inherit": "Add him — he has inherited a real job",
            "marginal": "Marginal — your call",
            "speculative": "Speculative — a bet on his role",
            "skip": "Not worth the roster spot",
            "hold": "No room, and nothing worth cutting",
        }[self.verdict]


class MoveAdvisor:
    """Prices any pickup against the roster it would join."""

    def __init__(
        self,
        league: LeagueWeek,
        pool: dict[str, Player],
        ctx: ContextEngine,
        horizon: dict[str, float],
        horizon_weeks: int = 4,
    ):
        self.league = league
        self.pool = pool
        self.ctx = ctx
        self.horizon = horizon
        self.horizon_weeks = horizon_weeks

        settings = league.settings
        self.slots = settings.starter_slots
        me = league.me

        mine = [pool[p] for p in me.player_ids if p in pool]
        self._adj = {p.player_id: ctx.adjust(p) for p in mine}
        self._week = [replace(p, proj_points=self._adj[p.player_id].points)
                      for p in mine]
        self._horizon = [self._as_horizon(p, self._adj[p.player_id]) for p in mine]
        self._base = lineup_mod.value(self._horizon, self.slots)

        # A player in either optimal lineup is not spare, or the advice would
        # tell you to start someone and cut him in the same breath.
        protected = {
            pl.player_id
            for players in (self._horizon, self._week)
            for _, pl in lineup_mod.optimize(players, self.slots).assignment
            if pl
        }
        by_value = {p.player_id: p.proj_points for p in self._horizon}
        self.droppable = sorted(
            (p for p in mine if p.player_id not in protected),
            key=lambda p: by_value.get(p.player_id, 0.0),
        )
        self._value = by_value

        self.counts: dict[str, int] = {}
        for p in mine:
            self.counts[p.position] = self.counts.get(p.position, 0) + 1
        self.floors = {
            pos: depth_floor(settings, pos) for pos in set(self.counts)
        }

        # An IR-listed player is not holding an active roster spot.
        capacity = sum(1 for s in settings.roster_slots if s not in _NON_ACTIVE)
        occupied = len(set(me.player_ids) - set(me.reserve))
        self.open_spots = max(0, capacity - occupied)

    def value_of(self, player: Player) -> float:
        """His adjusted value per week over the horizon."""
        return round(self._value.get(player.player_id, 0.0), 2)

    def depth_blocked(self, player: Player) -> bool:
        """Would cutting him leave the position unable to cover an absence?"""
        pos = player.position
        return self.counts.get(pos, 0) - 1 < self.floors.get(pos, 0)

    @property
    def safe_drops(self) -> list[Player]:
        """Spare players whose loss does not expose a position."""
        return [p for p in self.droppable if not self.depth_blocked(p)]

    def pick_drop(self, candidate: Player) -> tuple[Player | None, str]:
        """Cheapest cut that does not thin a position, and who was spared.

        Swapping like for like is always allowed: dropping a running back to
        add a running back leaves the count where it was.
        """
        spared = ""
        for p in self.droppable:
            if p.position == candidate.position or not self.depth_blocked(p):
                return p, spared
            if not spared:
                spared = p.name
        return None, spared

    def _as_horizon(self, p: Player, adj) -> Player:
        return replace(
            p,
            proj_points=round(
                self.horizon.get(p.player_id, 0.0)
                * adj.multiplier_excluding("matchup"),
                2,
            ),
        )

    def _never_plays(self, candidate: Player, lineup_gain: float) -> bool:
        """Would he sit behind a starter at a position with no way onto the field?

        The same rule `depth_floor` applies to drops: where the floor is just
        the dedicated slots (K, DEF, a QB outside superflex), a spare is a
        wasted roster spot, because the position cannot flex and is streamed
        the week a starter is out. RB, WR and TE backups are not covered —
        they fill FLEX and bye weeks, so their depth is worth something.
        """
        # Any gain means he enters the lineup. WORTH_IT is the bar for
        # recommending the move, not for whether he plays: with it here, Matt
        # Gay, 0.9 better than the starting kicker, was told he never plays.
        if lineup_gain > 0:
            return False
        pos = candidate.position
        dedicated = self.league.settings.slot_counts().get(pos, 0)
        return (dedicated > 0
                and depth_floor(self.league.settings, pos) == dedicated
                and self.counts.get(pos, 0) >= dedicated)

    def evaluate(
        self,
        candidate: Player,
        speculative: bool = False,
        vacated: Player | None = None,
        vacated_value: float | None = None,
    ) -> Move:
        adj = self.ctx.adjust(candidate)
        incoming = self._as_horizon(candidate, adj)
        add_value = incoming.proj_points
        lineup_gain = round(
            lineup_mod.value(self._horizon + [incoming], self.slots) - self._base, 2
        )

        drop, spared = self.pick_drop(candidate)
        drop_value = self._value.get(drop.player_id, 0.0) if drop else 0.0

        if vacated is None:
            vacated_value = 0.0
        elif vacated_value is None:
            vacated_value = round(
                self.horizon.get(vacated.player_id, vacated.proj_points), 2
            )
        vacated_value = round(vacated_value, 2)
        move = Move(
            add=candidate, add_value=round(add_value, 2),
            vacated_name=vacated.name if vacated else "",
            vacated_value=vacated_value,
            drop=None if self.open_spots > 0 else drop,
            drop_value=0.0 if self.open_spots > 0 else round(drop_value, 2),
            open_spots=self.open_spots, lineup_gain=lineup_gain,
            never_plays=self._never_plays(candidate, lineup_gain),
            verdict="skip", speculative=speculative,
            kept_for_depth="" if self.open_spots > 0 else spared,
        )
        move.verdict = self._verdict(move)
        move.lines = self._explain(move)
        return move

    def _verdict(self, m: Move) -> str:
        if m.open_spots > 0:
            # Nothing to weigh against: a free spot makes any real value a yes.
            if m.never_plays:
                return "marginal"
            return "add" if m.add_value > 0.5 or m.speculative else "marginal"
        if m.drop is None:
            return "hold"
        if m.never_plays:
            return "skip"
        if m.lineup_gain >= WORTH_IT or m.net >= CLEAR:
            return "add"
        if m.net >= WORTH_IT:
            return "worth_it"
        # A genuinely startable job just came free and this is the man who
        # inherits it. His own projection has not caught up, so the arithmetic
        # looks bad; the job is the point.
        if m.vacated_value >= REAL_JOB:
            return "inherit"
        if m.speculative:
            return "speculative"
        return "marginal" if m.net > 0 else "skip"

    def _explain(self, m: Move) -> list[str]:
        weeks = self.horizon_weeks
        out = [f"{m.add.name} is worth ~{m.add_value:.1f} pts/wk over the "
               f"next {weeks} weeks."]

        if m.open_spots > 0:
            out.append(
                f"You have {m.open_spots} open roster "
                f"{'spot' if m.open_spots == 1 else 'spots'} — nobody has to go."
            )
        elif m.drop is None:
            out.append(
                "There is no safe cut: everyone is either in your best lineup "
                "or the last body covering his position. Only make this move "
                "if you are willing to take that on."
            )
        else:
            out.append(
                f"Drop {m.drop.name} ({m.drop.position}, ~{m.drop_value:.1f}/wk) "
                f"— your cheapest cut that does not thin a position."
            )
            sign = "+" if m.net >= 0 else ""
            out.append(f"Net {sign}{m.net:.1f} pts/wk.")
        if m.kept_for_depth:
            out.append(
                f"Keeping {m.kept_for_depth}: he scores less, but cutting him "
                f"would leave you unable to cover a bye or an injury at his "
                f"position."
            )

        if m.never_plays:
            out.append(f"He would not start for you, and a backup {m.add.position} "
                       f"never plays in this league — pick one up the week "
                       f"you need him.")
        elif m.lineup_gain >= WORTH_IT:
            out.append(f"He would start for you: +{m.lineup_gain:.1f}/wk to your "
                       "best lineup.")
        elif m.vacated_name and m.vacated_value > 0:
            # His projection was drawn while he was a backup, so quoting the
            # net as settled would understate exactly the moves worth making.
            # Rather than invent a workload split, show the size of the job
            # that just came free and let the reader weigh it.
            out.append(
                f"That net is on today's projection, which still has him as a "
                f"backup — it predates the promotion. {m.vacated_name} was "
                f"worth ~{m.vacated_value:.1f}/wk, and that workload is now "
                f"unassigned."
            )
        elif m.speculative:
            out.append("He does not crack your lineup yet — the case is his "
                       "role, not his projection.")
        return out
