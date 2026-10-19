"""The brain: what should I take with this pick?

The ranking metric is VONA — value over next available. Taking a player is
worth what he adds to my starting lineup *now*, minus what I could expect from
that same position at my next pick. That is what makes the engine take the
last elite TE over a marginally better RB when six good RBs will survive the
turn, without any hand-written positional rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ffm.engine import lineup as lineup_mod
from ffm.engine import replacement as repl_mod
from ffm.engine import survival, tiers
from ffm.models import FLEX_ELIGIBILITY, DraftState, Player

# Positions that should not be drafted before the endgame.
_LATE_ONLY = ("K", "DEF")

# How much a bench player at depth N behind the starters is actually worth.
# A first backup RB plays real snaps; a fourth QB in a 1QB league never does.
_DEPTH_VALUE = (0.32, 0.14, 0.06, 0.02)

# Bench spots worth carrying at each position, beyond the starters. QB and TE
# are streamable in single-start formats, so surplus there is largely illusory;
# RB and WR churn through injuries and byes all season.
_BENCH_ALLOWANCE = {"QB": 1, "TE": 1, "RB": 4, "WR": 4, "K": 0, "DEF": 0}

# How often depth at a position actually gets used. Running backs miss time and
# lose workloads far more than receivers do, so an RB bench is worth more than
# the same nominal surplus at WR. A backup QB in a single-QB league almost
# never plays. Without this the flex distorts everything: filling FLEX with a
# WR counts WR as three "starters" against RB's two, and the engine then
# stacks receivers while leaving the RB room bare.
_DEPTH_NEED = {"RB": 1.40, "WR": 1.00, "TE": 0.60, "QB": 0.30}

# Bodies worth having at each position by the end of a standard draft.
# Marginal lineup value alone does not produce these: in half-PPR the next
# receiver almost always grades out above the next back, so a purely greedy
# board stacks six or seven WRs and leaves the RB room one injury from
# collapse. These are a floor on roster construction, not a target to hit.
_TARGET_COUNT = {"RB": 5, "WR": 5, "TE": 2, "QB": 1}

# Fractions of a player's value written off for injury. These were far too
# soft: a Questionable tag cost 3%, which is nothing, so flagged players kept
# surfacing at the top of the board.
_INJURY_PENALTY = {
    "IR": 0.90, "PUP": 0.75, "Out": 0.65, "Sus": 0.60,
    "Doubtful": 0.40, "Questionable": 0.12,
}


@dataclass
class Recommendation:
    player: Player
    score: float
    now_gain: float
    vona: float
    survival: float   # P(lasts from my pick to my following pick)
    hold: float       # P(still on the board when I actually pick)
    tier_left: int
    reasons: list[str] = field(default_factory=list)


@dataclass
class PositionOutlook:
    position: str
    best_now: float
    expected_next: float
    dropoff: float
    starters_needed: int


@dataclass
class Advice:
    state: DraftState
    available: list[Player]
    my_players: list[Player]
    recommendations: list[Recommendation]
    outlook: list[PositionOutlook]
    likely_gone: list[tuple[Player, float]]
    is_my_turn: bool
    realistic: Recommendation | None
    lineup: lineup_mod.Lineup
    needs: list[str]
    this_pick: int | None
    next_pick: int | None
    replacement: dict[str, float]
    projected_starters: float


def _candidate_set(available: list[Player], per_pos: int = 30, overall: int = 140) -> list[Player]:
    """Trim the board to plausible picks, keeping positional depth intact."""
    ranked = sorted(available, key=lambda p: (-p.vor, p.adp))
    chosen = {p.player_id: p for p in ranked[:overall]}
    by_pos: dict[str, int] = {}
    for p in ranked:
        n = by_pos.get(p.position, 0)
        if n < per_pos:
            chosen[p.player_id] = p
            by_pos[p.position] = n + 1
    return list(chosen.values())


def _unfilled_slots(my_players: list[Player], slots: list[str]) -> list[str]:
    filled = lineup_mod.optimize(my_players, slots)
    return [slot for slot, pl in filled.assignment if pl is None]


def position_cap(pos: str, settings) -> int:
    """Most players at a position worth rostering in this format."""
    counts = settings.slot_counts()
    starters = counts.get(pos, 0)
    if pos == "QB":
        starters += counts.get("SUPER_FLEX", 0)
    elif pos in ("RB", "WR"):
        # Only RB/WR realistically soak up flex spots week to week; crediting
        # TE with the flex too would license a third tight end.
        starters += counts.get("FLEX", 0) + counts.get("WRRB_FLEX", 0)
    return max(1, starters + _BENCH_ALLOWANCE.get(pos, 2))


def _bench_value(
    player: Player,
    my_players: list[Player],
    starters_by_pos: dict[str, int],
    replacement: dict[str, float],
    settings,
) -> tuple[float, bool]:
    """What a pick is worth purely as depth, and whether it busts the cap.

    Once the starting lineup is full every candidate has zero marginal starter
    value, so without this the board collapses to raw VOR and happily drafts a
    fourth quarterback. Two corrections matter:

    * Scale by how many I start at the position — a WR3 covers two or three
      weekly slots, a backup QB covers one.
    * Cap the position. A backup QB's surplus over QB12 looks large, but in a
      single-QB league you stream that from waivers for free, so the surplus is
      not really claimable.
    """
    have = sum(1 for p in my_players if p.position == player.position)
    if have >= position_cap(player.position, settings):
        return 0.0, True

    surplus = max(0.0, player.proj_points - replacement.get(player.position, 0.0))
    if surplus <= 0:
        return 0.0, False

    # Weight by how many lineup slots the position can fill, not by how many it
    # happens to fill right now — otherwise whoever currently holds the FLEX
    # gets credited with an extra starter and compounds into a stack.
    slots = sum(
        1 for s in settings.starter_slots
        if player.position in FLEX_ELIGIBILITY.get(s, (s,))
    )
    extra = max(0, have - starters_by_pos.get(player.position, 0))
    weight = (
        _DEPTH_VALUE[min(extra, len(_DEPTH_VALUE) - 1)]
        * _DEPTH_NEED.get(player.position, 0.8)
        * (max(1, slots) / 2.0)
    )
    return surplus * weight, False


# A player has to be at least this likely to reach my pick to be worth
# planning around.
_REACHABLE = 0.40

# ...and this unlikely before he is written off as gone. The gap between the
# two thresholds is deliberate: with them overlapping, a coin-flip player was
# simultaneously the recommended plan and listed as already gone.
_LIKELY_GONE = 0.30


def _first_realistic(recs: list[Recommendation]) -> Recommendation | None:
    """Highest-priority player with a real chance of reaching my pick."""
    for r in recs:
        if r.hold >= _REACHABLE:
            return r
    # Nobody is likely; fall back to whoever is most likely among the best.
    return max(recs[:12], key=lambda r: r.hold) if recs else None


def _bye_conflicts(player: Player, starters: list[Player]) -> int:
    if not player.bye_week:
        return 0
    return sum(1 for p in starters if p.bye_week == player.bye_week)


def analyze(
    state: DraftState, pool: dict[str, Player], bpa_weight: float = 0.20
) -> Advice:
    """Rank the board for the current pick.

    `bpa_weight` blends raw lineup improvement back into the pure VONA score.
    At 0 the engine is fully scarcity-driven; higher values pull it toward
    best-player-available. 0.2 keeps scarcity in charge while stopping it from
    reaching past a clearly better player for a marginal positional edge.
    """
    settings = state.settings
    slots = settings.starter_slots
    drafted = state.drafted_ids()

    all_players = list(pool.values())
    replacement = repl_mod.replacement_points(settings, all_players)
    repl_mod.apply_vor(all_players, replacement)
    tiers.assign_tiers(all_players)

    available = [p for p in all_players if p.player_id not in drafted]
    available.sort(key=lambda p: (-p.vor, p.adp))

    my_ids = {p.player_id for p in state.picks_by_slot(state.my_slot) if p.player_id}
    my_players = [pool[i] for i in my_ids if i in pool]

    my_pick_nums = state.my_pick_numbers(limit=3)
    this_pick = my_pick_nums[0] if my_pick_nums else None
    next_pick = my_pick_nums[1] if len(my_pick_nums) > 1 else None

    current_lineup = lineup_mod.optimize(my_players, slots)
    needs = _unfilled_slots(my_players, slots)

    ghosts = repl_mod.ghost_players(replacement)
    base_value = lineup_mod.value(my_players + ghosts, slots)

    candidates = _candidate_set(available)
    now_gain: dict[str, float] = {}
    for c in candidates:
        now_gain[c.player_id] = round(
            lineup_mod.value(my_players + [c] + ghosts, slots) - base_value, 2
        )

    # How much of the roster is still to come, and what must still be filled.
    picks_left = len(state.my_pick_numbers(limit=settings.rounds)) if this_pick else 0
    rounds_left = picks_left
    must_fill = [s for s in needs if s in _LATE_ONLY]

    # --- survival ---------------------------------------------------------
    # Two separate questions, and conflating them is what made every mid-round
    # player read as certain to survive while previewing an early pick:
    #   hold = will he still be on the board when I actually pick?
    #   surv = having got him to my pick, will he last one more turn?
    # Both are measured against the board as it stands right now, so the
    # frames line up. On my own turn hold is 1.0 and this reduces to the
    # simple case.
    is_my_turn = state.is_my_turn()
    current_pick = state.current_pick_no

    if this_pick and this_pick > current_pick:
        hold_prob = survival.availability(candidates, current_pick, this_pick)
    else:
        hold_prob = {c.player_id: 1.0 for c in candidates}

    if this_pick and next_pick:
        # Weighted by reach: the board at my next pick is not today's board.
        surv_prob = survival.availability(
            candidates, this_pick, next_pick, weights=hold_prob
        )
    else:
        surv_prob = {c.player_id: 0.0 for c in candidates}

    # P(still there at my *following* pick), measured from now.
    joint_prob = {
        c.player_id: hold_prob.get(c.player_id, 1.0) * surv_prob.get(c.player_id, 0.0)
        for c in candidates
    }

    expected_here: dict[str, float] = {}
    expected_next: dict[str, float] = {}
    for pos in {c.position for c in candidates}:
        pos_c = [c for c in candidates if c.position == pos]
        expected_here[pos] = round(
            survival.expected_best(
                [(now_gain[c.player_id], hold_prob.get(c.player_id, 1.0)) for c in pos_c]
            ), 2
        )
        expected_next[pos] = round(
            survival.expected_best(
                [(now_gain[c.player_id], joint_prob.get(c.player_id, 0.0)) for c in pos_c]
            ), 2
        )

    starters_only = [pl for _, pl in current_lineup.assignment if pl]
    starters_by_pos: dict[str, int] = {}
    for pl in starters_only:
        starters_by_pos[pl.position] = starters_by_pos.get(pl.position, 0) + 1

    recs: list[Recommendation] = []
    for c in candidates:
        gain = now_gain[c.player_id]
        vona = round(gain - expected_next.get(c.position, 0.0), 2)
        score = vona + bpa_weight * gain
        reasons: list[str] = []

        # The positional cap applies to every position, K and DEF included —
        # the bench branch below used to skip them, which is how a second
        # defense could take a roster spot that belonged to a running back.
        have_at_pos = sum(1 for pl in my_players if pl.position == c.position)
        if have_at_pos >= position_cap(c.position, settings):
            score -= 500.0
            reasons.append(f"already have enough {c.position}")

        # A pure bench pick adds nothing to the lineup today; value it as depth.
        elif gain <= 0.01 and c.position not in _LATE_ONLY:
            depth, over_cap = _bench_value(
                c, my_players, starters_by_pos, replacement, settings
            )
            if over_cap:
                score -= 500.0
                reasons.append(f"already have enough {c.position}")
            elif depth > 0:
                score += depth
                reasons.append(f"depth ({c.position}{c.pos_rank})")
            # Reward players sliding past their ADP.
            if this_pick and c.adp < 999 and this_pick - c.adp > 8:
                score += min(6.0, (this_pick - c.adp) * 0.12)
                reasons.append(f"falling (ADP {c.adp:.0f})")

        tier_left, next_tier = tiers.tier_context(c, available)
        surv = surv_prob.get(c.player_id, 0.0)
        hold = hold_prob.get(c.player_id, 1.0)

        # Last man in a tier, and unlikely to survive: this is the real signal.
        if tier_left <= 2 and surv < 0.45 and gain > 0:
            bonus = gain * 0.08 * (1.0 - surv)
            score += bonus
            reasons.append(f"last of {c.position} tier {c.tier} ({tier_left} left)")

        inj = _INJURY_PENALTY.get(c.injury_status or "", 0.0)
        if inj:
            score -= abs(gain) * inj
            reasons.append(f"injury: {c.injury_status}")

        conflicts = _bye_conflicts(c, starters_only)
        if conflicts >= 2:
            score -= 4.0 * (conflicts - 1)
            reasons.append(f"bye wk {c.bye_week} stacks {conflicts} starters")

        if c.position in _LATE_ONLY:
            if rounds_left > 2:
                score -= 500.0  # never before the endgame
                reasons.append("save for the last rounds")
            elif c.position in must_fill:
                score += 300.0
                reasons.append(f"must still fill {c.position}")

        # Endgame: if remaining picks barely cover required starters, prioritize.
        required_left = [s for s in needs if s not in _LATE_ONLY]
        if required_left and rounds_left <= len(required_left) + len(must_fill):
            eligible_for = [
                s for s in required_left
                if c.position in FLEX_ELIGIBILITY.get(s, (s,))
            ]
            if eligible_for:
                score += 25.0
                reasons.append(f"fills open {eligible_for[0]}")

        # Roster construction: pull toward the positional floors, weighted by
        # how few picks are left to fix a shortfall.
        shortfall = _TARGET_COUNT.get(c.position, 0) - have_at_pos
        if shortfall > 0 and rounds_left > 0 and c.position not in _LATE_ONLY:
            urgency = min(1.0, shortfall / rounds_left)
            bump = 16.0 * urgency * _DEPTH_NEED.get(c.position, 0.8)
            score += bump
            if urgency > 0.4:
                reasons.append(f"only {have_at_pos} {c.position} rostered")

        if not is_my_turn and hold < 0.5 and gain > 0:
            reasons.append(f"only {hold * 100:.0f}% to reach your pick")
        elif surv < 0.15 and gain > 0 and c.position not in _LATE_ONLY:
            reasons.append(f"{surv * 100:.0f}% to survive the turn")

        recs.append(
            Recommendation(
                player=c,
                score=round(score, 2),
                now_gain=gain,
                vona=vona,
                survival=round(surv, 3),
                hold=round(hold, 3),
                tier_left=tier_left,
                reasons=reasons,
            )
        )

    # A draft board is a priority list: you read down it and take the best guy
    # still on it. Weighting the order by availability inverts that — a player
    # who is 95% certain to reach you is one you can afford to *wait* on, not
    # one to rank second. So ordering stays pure priority, and gettability is
    # surfaced separately via `realistic` and the reach column.
    recs.sort(key=lambda r: -r.score)

    realistic = _first_realistic(recs) if not is_my_turn else (recs[0] if recs else None)

    outlook: list[PositionOutlook] = []
    for pos in ("QB", "RB", "WR", "TE"):
        pos_c = [c for c in candidates if c.position == pos]
        if not pos_c:
            continue
        best = expected_here.get(pos, 0.0)
        exp = expected_next.get(pos, 0.0)
        need = sum(
            1 for s in needs if pos in FLEX_ELIGIBILITY.get(s, (s,))
        )
        outlook.append(
            PositionOutlook(pos, round(best, 1), round(exp, 1), round(best - exp, 1), need)
        )
    outlook.sort(key=lambda o: -o.dropoff)

    # Before my turn, "gone" means gone before I pick; on my turn it means gone
    # over the coming turn.
    gone_metric = hold_prob if not is_my_turn else surv_prob
    # Never list the headline pick as gone. On a long turn nobody clears the
    # reachable bar, so the headline falls back to the most gettable player —
    # who would otherwise show up in both places at once.
    planned = realistic.player.player_id if realistic else None
    likely_gone = sorted(
        [
            (c, gone_metric.get(c.player_id, 1.0))
            for c in candidates
            if gone_metric.get(c.player_id, 1.0) < _LIKELY_GONE
            and c.vor > 0
            and c.player_id != planned
        ],
        key=lambda t: -t[0].vor,
    )[:8]

    return Advice(
        state=state,
        available=available,
        my_players=my_players,
        recommendations=recs,
        outlook=outlook,
        likely_gone=likely_gone,
        is_my_turn=is_my_turn,
        realistic=realistic,
        lineup=current_lineup,
        needs=needs,
        this_pick=this_pick,
        next_pick=next_pick,
        replacement=replacement,
        projected_starters=current_lineup.points,
    )
