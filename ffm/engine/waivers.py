"""What to pick up, and who to drop for him.

The draft engine asks "what does this player add to my starting lineup?" and
that question does not change in October — only the candidate set does. Here
the pool is everyone nobody owns, and the baseline is my actual roster rather
than a hypothetical one.

Two things a pure projection ranking gets wrong about waivers, both fixed here:

* **It is not about Sunday.** A pickup is a multi-week asset, so candidates are
  valued on the average of the next few weeks' projections, not this one. The
  matchup signal is dropped from that view for the same reason.
* **Projections lag role changes.** The waiver player worth having is usually
  one whose situation changed last week — the backup who just took 70% of the
  snaps. His projection has not caught up and his box score may look ordinary,
  so usage trend and the league-wide add market are scored explicitly rather
  than waited for.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from ffm.data.context import Adjustment, ContextEngine, role_view
from ffm.engine import lineup as lineup_mod
from ffm.engine.roster_move import Move, MoveAdvisor
from ffm.models import FLEX_ELIGIBILITY, LeagueWeek, Player

# Candidates actually worth solving a lineup for. The pool is ~1300 players
# and almost all of them are unrostered for good reason.
_PER_POS = 18
_OVERALL = 90

# Score weights. Multi-week value leads; this week is a tiebreak.
_W_HORIZON = 1.0
_W_WEEK = 0.35

_BREAKOUT_CAP = 2.5
_MARKET_CAP = 2.0

# Below this a pickup is not worth a roster move.
MIN_SCORE = 0.5


@dataclass
class Pickup:
    player: Player
    adj: Adjustment
    horizon_points: float   # adjusted points per week over the horizon
    week_points: float      # adjusted points this week
    horizon_gain: float     # lineup improvement per week over the horizon
    week_gain: float        # lineup improvement this week
    breakout: float
    market: int             # leagues adding him in the last 48h
    score: float
    drop: Player | None = None
    why: list[str] = field(default_factory=list)
    move: Move | None = None   # whether to bother, and who goes

    @property
    def kind(self) -> str:
        """What sort of add this is, so the two never get confused."""
        if self.horizon_gain >= 1.0:
            return "upgrade"
        if self.breakout > 0.8 or self.market > 20000:
            return "breakout"
        return "stash"


@dataclass
class DropCandidate:
    player: Player
    horizon_points: float
    note: str = ""


@dataclass
class WaiverBoard:
    week: int
    horizon_weeks: int
    pickups: list[Pickup]
    drops: list[DropCandidate]
    stale_context: bool = False


def _market_bonus(count: int) -> float:
    """Add counts run from a handful to six figures, so score them on a log."""
    if count <= 0:
        return 0.0
    return min(_MARKET_CAP, 0.8 * math.log10(1.0 + count / 1000.0))


def _breakout_bonus(ctx: ContextEngine, player: Player) -> tuple[float, str]:
    trend = ctx.role.get(player.player_id)
    if not trend:
        return 0.0, ""
    shift, recent, earlier, metric = role_view(trend, player.position)
    if shift <= 0.08:
        return 0.0, ""
    bonus = min(_BREAKOUT_CAP, shift * 8.0)
    return bonus, (
        f"role jumped ({earlier * 100:.0f}% -> {recent * 100:.0f}% of {metric})"
    )


def _shortlist(available: list[Player], horizon: dict[str, float], market: dict[str, int]):
    """Trim to plausible adds, keeping positional depth and the market intact."""
    ranked = sorted(available, key=lambda p: -horizon.get(p.player_id, 0.0))
    chosen = {p.player_id: p for p in ranked[:_OVERALL]}
    seen: dict[str, int] = {}
    for p in ranked:
        n = seen.get(p.position, 0)
        if n < _PER_POS:
            chosen[p.player_id] = p
            seen[p.position] = n + 1
    # Anyone the rest of the world is adding gets evaluated whether or not the
    # projections have noticed him yet — that is the entire point.
    for p in available:
        if market.get(p.player_id, 0) > 5000:
            chosen[p.player_id] = p
    return list(chosen.values())


def build_board(
    league: LeagueWeek,
    pool: dict[str, Player],
    ctx: ContextEngine,
    horizon: dict[str, float],
    market: dict[str, int],
    horizon_weeks: int = 4,
    limit: int = 12,
    advisor: MoveAdvisor | None = None,
) -> WaiverBoard:
    settings = league.settings
    slots = settings.starter_slots
    me = league.me
    owned = league.rostered_ids()
    startable = {
        pos for slot in slots for pos in FLEX_ELIGIBILITY.get(slot, (slot,))
    }

    mine = [pool[p] for p in me.player_ids if p in pool]
    my_adj = {p.player_id: ctx.adjust(p) for p in mine}

    def week_scored(players, adjust):
        return [replace(p, proj_points=adjust[p.player_id].points) for p in players]

    def horizon_scored(players, adjust):
        return [
            replace(
                p,
                proj_points=round(
                    horizon.get(p.player_id, 0.0)
                    * adjust[p.player_id].multiplier_excluding("matchup"),
                    2,
                ),
            )
            for p in players
        ]

    my_week = week_scored(mine, my_adj)
    my_horizon = horizon_scored(mine, my_adj)
    base_week = lineup_mod.value(my_week, slots)
    base_horizon = lineup_mod.value(my_horizon, slots)

    # A player on IR/PUP/suspension is not a pickup — his role trend and his
    # add count both look excellent right up until you notice he cannot play.
    available = [
        p for p in pool.values()
        if p.player_id not in owned and p.position in startable
        and p.injury_status not in ("IR", "PUP", "Sus")
        and horizon.get(p.player_id, 0.0) > 0
    ]
    candidates = _shortlist(available, horizon, market)

    pickups: list[Pickup] = []
    for c in candidates:
        adj = ctx.adjust(c)
        c_week = replace(c, proj_points=adj.points)
        c_horizon = replace(
            c,
            proj_points=round(
                horizon.get(c.player_id, 0.0) * adj.multiplier_excluding("matchup"), 2
            ),
        )
        week_gain = round(lineup_mod.value(my_week + [c_week], slots) - base_week, 2)
        horizon_gain = round(
            lineup_mod.value(my_horizon + [c_horizon], slots) - base_horizon, 2
        )
        breakout, breakout_note = _breakout_bonus(ctx, c)
        adds = market.get(c.player_id, 0)
        score = round(
            _W_HORIZON * horizon_gain
            + _W_WEEK * week_gain
            + breakout
            + _market_bonus(adds),
            2,
        )
        if score < MIN_SCORE:
            continue

        why = []
        if breakout_note:
            why.append(breakout_note)
        if adds > 5000:
            why.append(f"added in {adds:,} leagues in 48h")
        why.extend(adj.notes)
        pickups.append(
            Pickup(
                player=c,
                adj=adj,
                horizon_points=c_horizon.proj_points,
                week_points=c_week.proj_points,
                horizon_gain=horizon_gain,
                week_gain=week_gain,
                breakout=breakout,
                market=adds,
                score=score,
                why=why,
            )
        )

    pickups.sort(key=lambda p: -p.score)
    pickups = pickups[:limit]

    # One advisor answers "who goes" for every path, so the waiver board and
    # the push alerts can never disagree about it.
    advisor = advisor or MoveAdvisor(league, pool, ctx, horizon, horizon_weeks)
    # advisor.safe_drops is ordered by horizon value, so the column has to show
    # the same number — it was displaying this week's projection instead, which
    # made "worst first" look arbitrary.
    drops = [
        DropCandidate(player=p, horizon_points=advisor.value_of(p),
                      note=_drop_note(pool.get(p.player_id)))
        for p in advisor.safe_drops
    ][:6]
    for p in pickups:
        p.move = advisor.evaluate(p.player, speculative=p.kind != "upgrade")
        p.drop = p.move.drop

    return WaiverBoard(
        week=league.week,
        horizon_weeks=horizon_weeks,
        pickups=pickups,
        drops=drops,
        stale_context=ctx.stale,
    )


def _drop_note(player: Player | None) -> str:
    if player is None:
        return ""
    if player.injury_status in ("IR", "PUP"):
        return f"{player.injury_status} — check if your league has an IR slot first"
    if player.injury_status == "Out":
        return "currently Out"
    return ""
