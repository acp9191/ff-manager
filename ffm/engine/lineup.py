"""Optimal starting-lineup valuation.

Greedy slot-filling ("best QB to QB, then best leftover to FLEX") is not
optimal once SUPER_FLEX / REC_FLEX / WRRB_FLEX overlap, so this solves the
assignment exactly with the Hungarian algorithm. Rosters are tiny, so the
O(n^3) cost is irrelevant.
"""

from __future__ import annotations

from dataclasses import dataclass

from ffm.models import FLEX_ELIGIBILITY, Player

_INELIGIBLE = 1e6


def _hungarian(cost: list[list[float]]) -> tuple[float, list[int]]:
    """Min-cost assignment of every row to a distinct column (rows <= cols).

    Standard O(n^2 m) shortest-augmenting-path formulation. Returns the total
    cost and, for each row, the column it was assigned.
    """
    n, m = len(cost), len(cost[0])
    INF = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)   # column -> row
    way = [0] * (m + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], INF, -1
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j], way[j] = cur, j0
                if minv[j] < delta:
                    delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1

    assign = [-1] * n
    for j in range(1, m + 1):
        if p[j]:
            assign[p[j] - 1] = j - 1
    total = sum(cost[i][assign[i]] for i in range(n) if assign[i] >= 0)
    return total, assign


@dataclass
class Lineup:
    points: float
    assignment: list[tuple[str, Player | None]]  # (slot, player)


def optimize(players: list[Player], slots: list[str]) -> Lineup:
    """Best legal starting lineup from `players` for `slots`."""
    if not slots:
        return Lineup(0.0, [])
    n = len(slots)
    # Real players plus one empty-slot column per slot, so it always solves.
    cols: list[Player | None] = list(players) + [None] * n
    cost: list[list[float]] = []
    for slot in slots:
        eligible = FLEX_ELIGIBILITY.get(slot, (slot,))
        row = []
        for pl in cols:
            if pl is None:
                row.append(0.0)
            elif pl.position in eligible:
                row.append(-pl.proj_points)
            else:
                row.append(_INELIGIBLE)
        cost.append(row)

    total, assign = _hungarian(cost)
    out: list[tuple[str, Player | None]] = []
    pts = 0.0
    for i, slot in enumerate(slots):
        j = assign[i]
        pl = cols[j] if 0 <= j < len(cols) else None
        if pl is not None and cost[i][j] < _INELIGIBLE / 2:
            pts += pl.proj_points
            out.append((slot, pl))
        else:
            out.append((slot, None))
    return Lineup(round(pts, 2), out)


def value(players: list[Player], slots: list[str]) -> float:
    return optimize(players, slots).points
