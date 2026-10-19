"""Who to start this week.

Mechanically this is the draft's lineup optimizer pointed at weekly
projections instead of season ones — the same exact Hungarian assignment, so
overlapping flex slots stay correct. What is new is that it optimizes on
*adjusted* points: every player's projection is first run through the context
layer, so a soft matchup, a shrinking role or an offense that has been missing
its numbers all move the lineup, and the reason travels with the decision.

The output is deliberately a diff, not a lineup. You already have a lineup
set; what you want to know on Sunday morning is which two players to swap and
why, not to re-read nine names you were never going to change.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ffm.data.context import Adjustment, ContextEngine
from ffm.engine import lineup as lineup_mod
from ffm.models import FLEX_ELIGIBILITY, LeagueWeek, Player, Roster

# Below this the swap is inside the noise of any projection and telling you to
# make it is worse than useless — it invites churn and second-guessing.
MIN_SWAP_GAIN = 0.75

# Starting a game-time decision over a player who will definitely play needs a
# real edge. The health discount prices his average week; it does not price
# what happens when he is ruled out after the other games have locked: the
# slot scores zero and nobody is left to put in it. Week 4's "start Coker over
# Vele, +1.1" was exactly that — Coker was ruled out three hours later, and
# following it would have cost 14.4 points.
GAME_TIME = ("Questionable", "Doubtful")
RISKY_START_GAIN = 2.5


@dataclass
class Swap:
    slot: str
    start: Player
    sit: Player | None
    gain: float
    why: list[str] = field(default_factory=list)


@dataclass
class Alert:
    level: str  # urgent | warn
    text: str
    player_id: str | None = None


@dataclass
class Call:
    """One roster player: what he is worth this week and what moved him."""

    player: Player
    adj: Adjustment
    slot: str | None       # recommended slot, None = bench
    starting_now: bool

    @property
    def points(self) -> float:
        return self.adj.points


@dataclass
class WeekPlan:
    week: int
    season: str
    roster: Roster
    calls: list[Call]
    swaps: list[Swap]
    alerts: list[Alert]
    lineup: lineup_mod.Lineup
    current_points: float
    recommended_points: float
    opponent: Roster | None = None
    opponent_points: float = 0.0
    stale_context: bool = False

    # The same two lineups on raw projections — the numbers the Sleeper app
    # shows — so a matchup line can be checked against something you can see.
    current_raw: float = 0.0
    opponent_raw: float = 0.0
    locked_starters: int = 0   # starters whose game has already kicked off

    @property
    def gain(self) -> float:
        return round(self.recommended_points - self.current_points, 2)

    @property
    def followed_points(self) -> float:
        """What you project to if you make exactly the swaps reported.

        `recommended_points` is the optimizer's lineup, which also includes
        changes too small to report. Quoting it next to "lineup is already
        optimal" promised points no reported advice would get you.
        """
        return round(self.current_points + sum(s.gain for s in self.swaps), 2)

    @property
    def followed_raw(self) -> float:
        """`followed_points` on raw projections."""
        base = {c.player.player_id: c.adj.base for c in self.calls}
        moved = sum(
            base.get(s.start.player_id, 0.0) - (base.get(s.sit.player_id, 0.0) if s.sit else 0.0)
            for s in self.swaps
        )
        return round(self.current_raw + moved, 2)

    def by_slot(self) -> list[tuple[str, Call | None]]:
        index = {c.player.player_id: c for c in self.calls}
        out: list[tuple[str, Call | None]] = []
        for slot, pl in self.lineup.assignment:
            out.append((slot, index.get(pl.player_id) if pl else None))
        return out

    @property
    def bench(self) -> list[Call]:
        return sorted(
            (c for c in self.calls if c.slot is None), key=lambda c: -c.points
        )


def _adjusted(players: list[Player], ctx: ContextEngine) -> dict[str, Adjustment]:
    return {p.player_id: ctx.adjust(p) for p in players}


def _optimize_around_locks(
    scored: list[Player],
    slots: list[str],
    current: list[tuple[str, Player | None]],
    locked: frozenset[str] | set[str],
) -> lineup_mod.Lineup:
    """Best lineup that leaves every kicked-off player where he is."""
    fixed = {i: pl for i, (_, pl) in enumerate(current)
             if pl is not None and pl.player_id in locked}
    free = [s for i, s in enumerate(slots) if i not in fixed]
    movable = [p for p in scored if p.player_id not in locked]
    rest = iter(lineup_mod.optimize(movable, free).assignment)
    assignment = [(slot, fixed[i]) if i in fixed else next(rest)
                  for i, slot in enumerate(slots)]
    points = round(sum(pl.proj_points for _, pl in assignment if pl), 2)
    return lineup_mod.Lineup(points, assignment)


def _assign_current(roster: Roster, slots: list[str], pool: dict[str, Player]):
    """Sleeper's `starters` array is positionally aligned with the slot list."""
    out: list[tuple[str, Player | None]] = []
    for i, slot in enumerate(slots):
        pid = roster.starters[i] if i < len(roster.starters) else None
        out.append((slot, pool.get(pid) if pid else None))
    return out


def plan_week(
    league: LeagueWeek,
    pool: dict[str, Player],
    ctx: ContextEngine,
    opponent: Roster | None = None,
    locked: frozenset[str] | set[str] = frozenset(),
) -> WeekPlan:
    """Recommend a lineup for `league.me` and diff it against what is set.

    `locked` holds players whose game has kicked off. Sleeper will not move
    them, so neither may the advice: a locked starter keeps his slot, a locked
    bench player cannot come in, and nothing is said about either. Without
    this the watcher saw Jalen Coker go Out mid-game and told you, twice, to
    bench a player who had already played.
    """
    settings = league.settings
    slots = settings.starter_slots
    me = league.me
    week = league.week

    mine = [pool[p] for p in me.player_ids if p in pool]
    adjustments = _adjusted(mine, ctx)
    # Optimize on adjusted points by handing the optimizer a rescored copy;
    # the originals keep their raw projection so the report can show both.
    scored = [replace(p, proj_points=adjustments[p.player_id].points) for p in mine]
    by_id = {p.player_id: p for p in scored}

    current = _assign_current(me, slots, by_id)
    best = _optimize_around_locks(scored, slots, current, locked)
    slot_of = {pl.player_id: slot for slot, pl in best.assignment if pl}

    current_points = round(sum(pl.proj_points for _, pl in current if pl), 2)
    cur_ids = {pl.player_id for _, pl in current if pl}

    calls = [
        Call(
            player=p,
            adj=adjustments[p.player_id],
            slot=slot_of.get(p.player_id),
            starting_now=p.player_id in cur_ids,
        )
        for p in mine
    ]

    swaps = _diff(best.assignment, current, by_id, adjustments)
    alerts = _alerts(me, current, pool, week, settings.starter_slots, locked)

    current_raw = round(sum(pool[pl.player_id].proj_points
                            for _, pl in current if pl and pl.player_id in pool), 2)
    opp_points = opp_raw = 0.0
    if opponent:
        opp_players = [pool[p] for p in opponent.starters if p in pool]
        opp_adj = _adjusted(opp_players, ctx)
        opp_points = round(
            sum(opp_adj[p.player_id].points for p in opp_players), 2
        )
        opp_raw = round(sum(p.proj_points for p in opp_players), 2)

    return WeekPlan(
        week=week,
        season=league.season,
        roster=me,
        calls=calls,
        swaps=swaps,
        alerts=alerts,
        lineup=best,
        current_points=current_points,
        recommended_points=best.points,
        opponent=opponent,
        opponent_points=opp_points,
        stale_context=ctx.stale,
        current_raw=current_raw,
        opponent_raw=opp_raw,
        locked_starters=len(cur_ids & set(locked)),
    )


def _diff(
    recommended: list[tuple[str, Player | None]],
    current: list[tuple[str, Player | None]],
    scored: dict[str, Player],
    adjustments: dict[str, Adjustment],
) -> list[Swap]:
    """Real changes only.

    Two players trading the RB2 and FLEX labels is not a decision, so the diff
    is taken over who is starting rather than over slot assignments.
    """
    rec_ids = {pl.player_id for _, pl in recommended if pl}
    cur_ids = {pl.player_id for _, pl in current if pl}
    coming_in = [(slot, pl) for slot, pl in recommended if pl and pl.player_id not in cur_ids]
    going_out = [pl for _, pl in current if pl and pl.player_id not in rec_ids]

    # Match same-position players first, across the whole diff rather than
    # greedily per incoming player. Otherwise the highest-scoring arrival
    # claims the injured back and the report says "start this WR instead of
    # that RB" — legal in a FLEX, but it reads as a mistake to anyone acting
    # on it, and an injury alert then names the wrong replacement.
    leaving = list(going_out)
    pairs: list[tuple[str, Player, Player | None]] = []
    unmatched: list[tuple[str, Player]] = []
    for slot, incoming in sorted(coming_in, key=lambda t: -t[1].proj_points):
        same = next((p for p in leaving if p.position == incoming.position), None)
        if same:
            leaving.remove(same)
            pairs.append((slot, incoming, same))
        else:
            unmatched.append((slot, incoming))

    for slot, incoming in unmatched:
        eligible = FLEX_ELIGIBILITY.get(slot, (slot,))
        pick = next(
            (p for p in leaving if p.position in eligible),
            leaving[0] if leaving else None,
        )
        if pick:
            leaving.remove(pick)
        pairs.append((slot, incoming, pick))

    swaps: list[Swap] = []
    for slot, incoming, pick in pairs:
        gain = round(incoming.proj_points - (pick.proj_points if pick else 0.0), 2)
        risky = (incoming.injury_status in GAME_TIME
                 and not (pick and pick.injury_status in GAME_TIME))
        if gain < (RISKY_START_GAIN if risky else MIN_SWAP_GAIN):
            continue
        why = _reasons(incoming, pick, adjustments)
        swaps.append(Swap(slot=slot, start=incoming, sit=pick, gain=gain, why=why))
    return sorted(swaps, key=lambda s: -s.gain)


def _reasons(
    incoming: Player, pick: Player | None, adjustments: dict[str, Adjustment]
) -> list[str]:
    """Why to make the swap, the case for it first.

    Ranking by magnitude alone put the incoming player's biggest signal first
    even when it pointed the other way, and the digest shows only the first
    line: "start Loveland · tough matchup vs MIN". A signal argues for the swap
    when it lifts the incoming player or sinks the one being benched; the rest
    follow as caveats, so nothing is hidden.
    """
    inc = adjustments[incoming.player_id]
    out = adjustments[pick.player_id] if pick else None
    pro = [(s.delta, s.note) for s in inc.signals if s.delta > 0]
    con = [(-s.delta, s.note) for s in inc.signals if s.delta < 0]
    if pick and out:
        pro += [(-s.delta, f"{pick.name}: {s.note}") for s in out.signals if s.delta < 0]
        con += [(s.delta, f"{pick.name}: {s.note}") for s in out.signals if s.delta > 0]

    lines: list[str] = []
    # Team, matchup and role are not priced, so unless an injury or your own
    # read is behind it the projection gap is the reason; lead with it.
    if pick and out and inc.base - out.base >= MIN_SWAP_GAIN:
        lines.append(f"projected {inc.base:.1f} to {out.base:.1f}")
    lines += [n for _, n in sorted(pro, key=lambda t: -t[0])]
    lines += [f"despite: {n}" for _, n in sorted(con, key=lambda t: -t[0])]
    return lines


def _alerts(
    roster: Roster,
    current: list[tuple[str, Player | None]],
    pool: dict[str, Player],
    week: int,
    slots: list[str],
    locked: frozenset[str] | set[str] = frozenset(),
) -> list[Alert]:
    """Things that are wrong right now, regardless of any optimization.

    A starter whose game has kicked off is past fixing, so an injury that
    happens during it is news, not a lineup problem.
    """
    out: list[Alert] = []
    for slot, pl in current:
        if pl is not None and pl.player_id in locked:
            continue
        if pl is None:
            out.append(Alert("urgent", f"{slot} is empty — you will score 0 there"))
            continue
        real = pool.get(pl.player_id, pl)
        pid = real.player_id
        if real.bye_week == week:
            out.append(Alert("urgent", f"{real.name} is on BYE and in your {slot}", pid))
        elif real.injury_status in ("Out", "IR", "PUP", "Sus"):
            out.append(Alert(
                "urgent", f"{real.name} is {real.injury_status} and in your {slot}", pid
            ))
        elif real.injury_status == "Doubtful":
            out.append(Alert("warn", f"{real.name} is Doubtful and in your {slot}", pid))
        elif real.injury_status == "Questionable":
            out.append(Alert(
                "warn", f"{real.name} is Questionable — check inactives", pid
            ))
    return out
