"""Turn raw projected stat lines into league-specific fantasy points.

Sleeper's scoring_settings keys are the same identifiers as the projection
stat keys (rec, rec_yd, pass_td, ...), so a league's exact scoring is just a
dot product. That beats bucketing every league into ppr/half/std, and it picks
up TE premium, first-down scoring, and per-yard tweaks for free.
"""

from __future__ import annotations

# Keys in scoring_settings that are league config, not per-stat multipliers.
_NON_STAT_KEYS = {
    "rec_yd_bonus", "pass_yd_bonus", "rush_yd_bonus",
    "idp_tkl", "idp_tkl_ast", "idp_tkl_solo", "idp_int", "idp_fum_rec",
    "idp_sack", "idp_safe", "idp_pass_def", "idp_blk_kick", "idp_def_td",
}

STANDARD_SCORING: dict[str, dict[str, float]] = {
    "ppr": {"rec": 1.0},
    "half_ppr": {"rec": 0.5},
    "std": {"rec": 0.0},
}

# Baseline used when a platform gives us no usable scoring settings.
_BASE = {
    "pass_yd": 0.04, "pass_td": 4.0, "pass_int": -2.0, "pass_2pt": 2.0,
    "rush_yd": 0.1, "rush_td": 6.0, "rush_2pt": 2.0,
    "rec_yd": 0.1, "rec_td": 6.0, "rec_2pt": 2.0,
    "fum_lost": -2.0,
    "xpm": 1.0, "fgm_0_19": 3.0, "fgm_20_29": 3.0, "fgm_30_39": 3.0,
    "fgm_40_49": 4.0, "fgm_50p": 5.0,
    # Team defense. Without these a DEF stat line dot-products to zero, which
    # the season path hid behind the fallback below but weekly scoring does not.
    "sack": 1.0, "int": 2.0, "fum_rec": 2.0, "def_td": 6.0, "safe": 2.0,
    "blk_kick": 2.0, "def_st_td": 6.0, "st_td": 6.0,
    "pts_allow_0": 10.0, "pts_allow_1_6": 7.0, "pts_allow_7_13": 4.0,
    "pts_allow_14_20": 1.0, "pts_allow_21_27": 0.0, "pts_allow_28_34": -1.0,
    "pts_allow_35p": -4.0,
}


def default_scoring(scoring_type: str = "half_ppr") -> dict[str, float]:
    s = dict(_BASE)
    s.update(STANDARD_SCORING.get(scoring_type, STANDARD_SCORING["half_ppr"]))
    return s


def classify(scoring: dict[str, float]) -> str:
    """Label a scoring dict for display purposes."""
    rec = float(scoring.get("rec", 0.0) or 0.0)
    te = float(scoring.get("bonus_rec_te", 0.0) or 0.0)
    base = "ppr" if rec >= 0.9 else "half_ppr" if rec >= 0.4 else "std"
    return f"{base}+te_prem" if te >= 0.25 else base


def compute_points(stats: dict[str, float], scoring: dict[str, float]) -> float:
    """Dot-product the projected stat line with the league's multipliers."""
    if not stats:
        return 0.0
    total = 0.0
    for key, mult in scoring.items():
        if not mult or key in _NON_STAT_KEYS or key.startswith("adp") or key.startswith("pts_"):
            continue
        val = stats.get(key)
        if val:
            total += float(val) * float(mult)
    return round(total, 2)


def fallback_points(stats: dict[str, float], scoring_type: str) -> float:
    """Sleeper's own precomputed column, for when custom scoring is unusable."""
    key = {"ppr": "pts_ppr", "half_ppr": "pts_half_ppr", "std": "pts_std"}.get(
        scoring_type.split("+")[0], "pts_half_ppr"
    )
    return round(float(stats.get(key, 0.0) or 0.0), 2)


def score_player(
    stats: dict[str, float],
    scoring: dict[str, float],
    scoring_type: str,
    min_baseline: float = 20.0,
) -> float:
    """Custom scoring when it looks sane, otherwise the precomputed column.

    Guard rail: a scoring dict missing the core yardage/TD multipliers (some
    platforms hand back a partial settings object) would produce a wildly low
    total and silently wreck every ranking, so we sanity-check against
    Sleeper's own number before trusting it.

    `min_baseline` is how big Sleeper's own number has to be before a shortfall
    counts as evidence of a broken scoring dict rather than just an
    unremarkable player. It is a season total by default; weekly callers pass
    a weekly-sized floor, without which the guard can never fire and a scoring
    gap turns into a silent zero.
    """
    if not scoring:
        return fallback_points(stats, scoring_type)
    custom = compute_points(stats, scoring)
    baseline = fallback_points(stats, scoring_type)
    if baseline > min_baseline and custom < baseline * 0.5:
        return baseline
    return custom
