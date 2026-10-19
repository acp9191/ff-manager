"""Will he still be there at my next pick?

Each player's true draft slot is modelled as Normal(ADP, sigma). Two details
matter more than the distribution choice:

1. Conditioning. A player with ADP 12 who is still on the board at pick 40 is
   a faller; the unconditional model says he is gone, which is exactly wrong.
   We condition on "available now".
2. Calibration. Exactly N players come off the board between my picks, so the
   per-player take-probabilities are rescaled to sum to N. That also absorbs a
   room drafting ahead of or behind ADP.
"""

from __future__ import annotations

import math

from ffm.models import Player


def _phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def sigma_for(adp: float) -> float:
    """ADP uncertainty widens deeper into the draft."""
    return max(3.0, min(45.0, 2.5 + 0.30 * adp))


def raw_taken_prob(adp: float, current_pick: int, target_pick: int) -> float:
    """P(taken before `target_pick` | still available at `current_pick`)."""
    if adp >= 999:
        # Undrafted-in-ADP players: treat as very likely to last.
        return 0.02
    s = sigma_for(adp)
    surv_now = 1.0 - _phi((current_pick - 0.5 - adp) / s)
    surv_then = 1.0 - _phi((target_pick - 0.5 - adp) / s)
    if surv_now <= 1e-6:
        # Already well past his ADP — he is a faller; use a mild hazard so he
        # is not treated as certain to last.
        gap = max(1, target_pick - current_pick)
        return min(0.95, 1.0 - math.exp(-gap / 25.0))
    return max(0.0, min(1.0, 1.0 - surv_then / surv_now))


def availability(
    candidates: list[Player],
    current_pick: int,
    target_pick: int,
    weights: dict[str, float] | None = None,
) -> dict[str, float]:
    """P(available at `target_pick` | available at `current_pick`) per player.

    `weights` is the probability each player is even on the board at
    `current_pick`. It matters only for calibration, and it matters a lot: when
    looking ahead from a future pick, the candidate list still contains players
    who will be long gone by then. Counting them as "about to be drafted"
    inflates the expected number of picks — 15 expected takes across a 7-pick
    window in a real draft — and the correction then crushes genuinely
    uncertain players into false certainty.
    """
    n_between = max(0, target_pick - current_pick)
    if n_between <= 0:
        return {p.player_id: 1.0 for p in candidates}

    w = weights or {}
    raw = {p.player_id: raw_taken_prob(p.adp, current_pick, target_pick) for p in candidates}

    # Calibrate so the expected number of players taken matches the number of
    # picks that actually happen. Only meaningful when the candidate list
    # covers most of what will come off the board.
    effective_board = sum(w.get(pid, 1.0) for pid in raw)
    exponent = 1.0
    if effective_board >= 2 * n_between and sum(raw.values()) > 1e-9:
        exponent = _calibration_exponent(raw, n_between, w)

    return {
        pid: max(0.0, min(1.0, 1.0 - pr ** exponent)) for pid, pr in raw.items()
    }


def _calibration_exponent(
    raw: dict[str, float], target: float, weights: dict[str, float]
) -> float:
    """Find e with sum(weight * p**e) == target.

    Scaling probabilities by a constant is the obvious approach and it is
    wrong: it drags players who are certainly gone (p == 1) back to a uniform
    survival chance, so every elite player reads the same. Raising to a power
    keeps 0 and 1 fixed, preserves ordering, and still moves the mass.
    """
    items = [(pr, weights.get(pid, 1.0)) for pid, pr in raw.items()]

    def total(e: float) -> float:
        return sum(wt * (pr ** e) for pr, wt in items)

    lo, hi = 0.05, 20.0
    if total(hi) > target:
        return hi
    if total(lo) < target:
        return lo
    for _ in range(40):
        mid = (lo + hi) / 2
        if total(mid) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def expected_best(values: list[tuple[float, float]]) -> float:
    """E[max value] over (value, availability) pairs, assuming independence.

    Walk candidates best-first: each contributes its value weighted by the
    chance it survives *and* everyone better than it does not.
    """
    total = 0.0
    none_so_far = 1.0
    for value, avail in sorted(values, key=lambda t: -t[0]):
        total += value * avail * none_so_far
        none_so_far *= 1.0 - avail
        if none_so_far < 1e-6:
            break
    return total


def board_drift(picks_adp: list[tuple[int, float]]) -> float:
    """Mean (pick_no - ADP) so far: negative = the room is reaching."""
    real = [(n, a) for n, a in picks_adp if a < 999]
    if len(real) < 6:
        return 0.0
    return sum(n - a for n, a in real) / len(real)
