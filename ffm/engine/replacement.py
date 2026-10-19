"""Replacement level: the point where a position stops being scarce.

Flex slots are allocated to whichever positions actually win them league-wide,
rather than assumed, so a league that flexes mostly WRs gets a deeper WR
baseline automatically.
"""

from __future__ import annotations

from ffm.models import FLEX_ELIGIBILITY, LeagueSettings, Player

_DEDICATED = ("QB", "RB", "WR", "TE", "K", "DEF")


def starter_demand(settings: LeagueSettings, pool: list[Player]) -> dict[str, int]:
    """How many of each position the whole league starts in a typical week."""
    counts = settings.slot_counts()
    teams = settings.teams

    demand = {pos: teams * counts.get(pos, 0) for pos in _DEDICATED}

    by_pos: dict[str, list[Player]] = {}
    for p in pool:
        by_pos.setdefault(p.position, []).append(p)
    for lst in by_pos.values():
        lst.sort(key=lambda p: -p.proj_points)

    # Everyone not already claimed by a dedicated slot competes for flex spots.
    for slot, n in counts.items():
        if slot in _DEDICATED:
            continue
        eligible = FLEX_ELIGIBILITY.get(slot, ())
        pool_left: list[Player] = []
        for pos in eligible:
            pool_left.extend(by_pos.get(pos, [])[demand.get(pos, 0):])
        pool_left.sort(key=lambda p: -p.proj_points)
        for p in pool_left[: teams * n]:
            demand[p.position] = demand.get(p.position, 0) + 1

    return demand


def replacement_points(settings: LeagueSettings, pool: list[Player]) -> dict[str, float]:
    """Projected points of the first player past the last startable one."""
    demand = starter_demand(settings, pool)
    by_pos: dict[str, list[Player]] = {}
    for p in pool:
        by_pos.setdefault(p.position, []).append(p)

    out: dict[str, float] = {}
    for pos, players in by_pos.items():
        players.sort(key=lambda p: -p.proj_points)
        n = demand.get(pos, 0)
        if not players:
            out[pos] = 0.0
        elif n <= 0:
            out[pos] = players[0].proj_points
        else:
            # Average a small band around the baseline to damp projection noise.
            band = players[max(0, n - 1): n + 3] or players[-1:]
            out[pos] = sum(p.proj_points for p in band) / len(band)
    return out


def apply_vor(pool: list[Player], repl: dict[str, float]) -> None:
    for p in pool:
        p.vor = round(p.proj_points - repl.get(p.position, 0.0), 2)


def ghost_players(repl: dict[str, float], per_pos: int = 4) -> list[Player]:
    """Replacement-level placeholders that fill out a hypothetical roster.

    Valuing a pick against a roster already backfilled with replacement talent
    is what makes 'I have two elite RBs, a third adds little' fall out
    automatically instead of needing a hand-tuned positional-need fudge.
    """
    out: list[Player] = []
    for pos, pts in repl.items():
        for i in range(per_pos):
            out.append(
                Player(
                    player_id=f"__ghost_{pos}_{i}",
                    name=f"replacement {pos}",
                    position=pos,
                    proj_points=pts,
                )
            )
    return out
