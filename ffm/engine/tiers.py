"""Tier detection inside a position.

Tiers are where the real draft decisions live: taking the last player of a
tier is worth far more than being first into the next one. Breaks are found
from the distribution of point gaps rather than a fixed cutoff, so they adapt
to the league's scoring.
"""

from __future__ import annotations

import statistics

from ffm.models import Player


def assign_tiers(pool: list[Player], max_per_pos: int = 60) -> None:
    """Set `.tier` (1-based) on every player, per position."""
    by_pos: dict[str, list[Player]] = {}
    for p in pool:
        by_pos.setdefault(p.position, []).append(p)

    for players in by_pos.values():
        players.sort(key=lambda p: -p.proj_points)
        head = players[:max_per_pos]
        if len(head) < 3:
            for i, p in enumerate(players, 1):
                p.tier = i
            continue

        gaps = [head[i].proj_points - head[i + 1].proj_points for i in range(len(head) - 1)]
        positive = [g for g in gaps if g > 0]
        if not positive:
            for p in players:
                p.tier = 1
            continue

        med = statistics.median(positive)
        try:
            spread = statistics.pstdev(positive)
        except statistics.StatisticsError:
            spread = 0.0
        # A break is a gap that stands out against typical spacing.
        threshold = max(med * 2.0, med + 1.5 * spread, 4.0)

        tier = 1
        head[0].tier = 1
        for i in range(1, len(head)):
            if gaps[i - 1] >= threshold:
                tier += 1
            head[i].tier = tier
        for p in players[max_per_pos:]:
            p.tier = tier + 1


def tier_context(player: Player, available: list[Player]) -> tuple[int, int]:
    """(players left in this player's tier, size of the next tier down)."""
    same = [p for p in available if p.position == player.position and p.tier == player.tier]
    nxt = [p for p in available if p.position == player.position and p.tier == player.tier + 1]
    return len(same), len(nxt)
