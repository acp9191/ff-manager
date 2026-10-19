"""Internal-consistency checks for the recommendation engine.

The engine reasons across two points in the draft at once — the board now and
the board at your next pick — and mixing those frames is where its bugs have
actually come from. These invariants catch that class of error without anyone
having to eyeball a dashboard.
"""

from __future__ import annotations

import itertools
import math

from ffm.engine.recommend import _LIKELY_GONE, _REACHABLE, Advice


def check_advice(advice: Advice) -> list[str]:
    """Return a list of invariant violations for one draft state."""
    out: list[str] = []

    def bad(cond: bool, msg: str) -> None:
        if not cond:
            out.append(msg)

    exp_next = {o.position: o.expected_next for o in advice.outlook}
    exp_here = {o.position: o.best_now for o in advice.outlook}

    for r in advice.recommendations:
        pos = r.player.position
        if pos in exp_next:
            bad(
                abs(r.vona - (r.now_gain - exp_next[pos])) < 0.15,
                f"VONA mismatch for {r.player.name}",
            )
        bad(0.0 <= r.hold <= 1.0, f"hold out of range for {r.player.name}")
        bad(0.0 <= r.survival <= 1.0, f"survival out of range for {r.player.name}")
        bad(not math.isnan(r.score), f"NaN score for {r.player.name}")

    for pos in exp_next:
        bad(
            exp_here[pos] >= exp_next[pos] - 0.05,
            f"{pos}: waiting a turn is worth more than picking now",
        )

    # Within a position, a better ADP must not be more likely to survive.
    for pos in ("QB", "RB", "WR", "TE"):
        rs = sorted(
            (r for r in advice.recommendations
             if r.player.position == pos and r.player.adp < 999),
            key=lambda r: r.player.adp,
        )
        for a, b in itertools.pairwise(rs):
            bad(
                a.hold <= b.hold + 0.02,
                f"{pos}: {a.player.name} (ADP {a.player.adp:.0f}) survives more "
                f"than {b.player.name} (ADP {b.player.adp:.0f})",
            )

    scores = [r.score for r in advice.recommendations]
    bad(scores == sorted(scores, reverse=True), "board is not ordered by score")

    if advice.is_my_turn:
        bad(
            all(r.hold == 1.0 for r in advice.recommendations),
            "players are not certain to reach a pick that is happening now",
        )

    if advice.realistic:
        bad(advice.realistic in advice.recommendations, "headline is not on the board")
        gone = {p.player_id for p, _ in advice.likely_gone}
        bad(
            advice.realistic.player.player_id not in gone,
            f"headline {advice.realistic.player.name} is also listed as gone",
        )
        if not advice.is_my_turn and any(
            r.hold >= _REACHABLE for r in advice.recommendations
        ):
            bad(
                advice.realistic.hold >= _REACHABLE,
                f"headline {advice.realistic.player.name} is not realistically gettable",
            )

    bad(_LIKELY_GONE < _REACHABLE, "gone and reachable thresholds overlap")
    return out
