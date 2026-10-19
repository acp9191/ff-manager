"""In-season data: weekly projections, weekly results, and the schedule.

The draft path only ever needed season totals. Everything here is per-week,
which is what start/sit and waivers reason about — and, more importantly, it
is what makes the context layer possible. Comparing a week's actual
production against what was projected for *that same week* is a direct
measurement of where the projections are wrong, which is the only honest way
to answer "this team looks good on paper, but is it?".

Completed weeks never change, so they are cached hard. Only the current week
is refetched.
"""

from __future__ import annotations

import requests

from ffm import cache
from ffm.data import scoring as scoring_mod
from ffm.data.sleeper_data import all_players, bye_weeks, current_season, nfl_state
from ffm.models import SKILL_POSITIONS, LeagueSettings, Player

BASE = "https://api.sleeper.app"
TIMEOUT = 30

# One week of scoring is roughly a seventeenth of a season, so the sanity
# floor that guards against a broken scoring dict has to shrink with it.
WEEK_BASELINE = 3.0

LIVE_TTL = 3 * 3600
SETTLED_TTL = 30 * 24 * 3600
SCHEDULE_TTL = 7 * 24 * 3600
# Shorter than any sane poll interval, or a watcher diffing the add market
# against itself would see the same cached numbers twice and detect nothing.
TRENDING_TTL = 900


def set_freshness(seconds: int) -> None:
    """Shorten the live TTLs for a long-running watcher.

    A one-shot report can happily read three-hour-old projections. A process
    watching for injury news cannot: the whole point is to notice within
    minutes, and the current week's projections — which carry the freshest
    injury tag Sleeper exposes — are only ~0.2 MB, so refetching them often is
    close to free.
    """
    global LIVE_TTL, TRENDING_TTL
    from ffm.data import sleeper_data

    LIVE_TTL = max(60, seconds)
    TRENDING_TTL = max(60, seconds)
    # Depth charts change over days, not minutes, but they are what promotions
    # are read from, so they should not go a whole day stale either.
    sleeper_data.PLAYERS_TTL = max(3600, seconds * 6)

# Week 18 is played by resting starters and backups on teams with nothing left
# to win. Treating it as evidence about an offense is actively misleading, so
# it never enters the historical window.
EXCLUDE_WEEKS = (18,)


def _get(url: str, **params):
    r = requests.get(url, params=params or None, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def current_week() -> int:
    try:
        return int(nfl_state().get("week") or 1)
    except Exception:
        return 1


def _settled(season: str, week: int) -> bool:
    """Has this week finished, so its data will never change again?"""
    try:
        st = nfl_state()
    except Exception:
        return False
    now_season, now_week = str(st.get("season") or ""), int(st.get("week") or 1)
    if str(season) < now_season:
        return True
    return str(season) == now_season and week < now_week


def _week_ttl(season: str, week: int) -> int:
    return SETTLED_TTL if _settled(season, week) else LIVE_TTL


# ---- raw feeds ---------------------------------------------------------

def weekly_projections(season: str, week: int) -> list[dict]:
    def fetch():
        return _get(
            f"{BASE}/projections/nfl/{season}/{week}",
            season_type="regular",
            **{"position[]": list(SKILL_POSITIONS)},
        )

    return cache.cached(f"proj_{season}_w{week}", _week_ttl(season, week), fetch)


def weekly_stats(season: str, week: int) -> list[dict]:
    def fetch():
        return _get(
            f"{BASE}/stats/nfl/{season}/{week}",
            season_type="regular",
            **{"position[]": list(SKILL_POSITIONS)},
        )

    return cache.cached(f"stats_{season}_w{week}", _week_ttl(season, week), fetch)


def schedule(season: str) -> list[dict]:
    def fetch():
        return _get(f"{BASE}/schedule/nfl/regular/{season}")

    try:
        return cache.cached(f"schedule_{season}", SCHEDULE_TTL, fetch)
    except Exception:
        return []


def opponents(season: str) -> dict[int, dict[str, tuple[str, bool]]]:
    """week -> team -> (opponent, is_home). Teams on bye are simply absent."""
    out: dict[int, dict[str, tuple[str, bool]]] = {}
    for g in schedule(season):
        wk, home, away = g.get("week"), g.get("home"), g.get("away")
        if not wk or not home or not away:
            continue
        out.setdefault(int(wk), {})[home] = (away, True)
        out[int(wk)][away] = (home, False)
    return out


# Game status is what locks a lineup slot, so it cannot ride the week-long
# schedule cache: that copy still said "pre_game" while Jalen Coker was being
# helped off the field, and the watcher told you to bench him.
GAME_STATUS_TTL = 120
LOCKED = ("in_game", "complete")


def game_status(season: str, week: int) -> dict[str, str]:
    """team -> pre_game | in_game | complete for one week. Byes are absent."""
    def fetch():
        return _get(f"{BASE}/schedule/nfl/regular/{season}")

    try:
        games = cache.cached(f"gamestatus_{season}", GAME_STATUS_TTL, fetch)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for g in games or []:
        if g.get("week") != week:
            continue
        for side in ("home", "away"):
            if g.get(side):
                out[g[side]] = g.get("status") or ""
    return out


def trending(kind: str = "add", hours: int = 48, limit: int = 50) -> dict[str, int]:
    """player_id -> number of Sleeper leagues adding (or dropping) him.

    The only genuine market signal in the free API: it is every other manager's
    read on who just became relevant, which routinely moves before projections do.
    """
    def fetch():
        return _get(
            f"{BASE}/v1/players/nfl/trending/{kind}",
            lookback_hours=hours, limit=limit,
        )

    try:
        rows = cache.cached(f"trending_{kind}_{hours}_{limit}", TRENDING_TTL, fetch)
    except Exception:
        return {}
    return {str(r["player_id"]): int(r.get("count") or 0) for r in rows or []}


# ---- history window ----------------------------------------------------

def history_window(season: str, week: int, want: int = 5) -> list[tuple[str, int, float]]:
    """The most recent `want` completed weeks as (season, week, weight).

    Walks backwards from the current week and, when the current season cannot
    supply enough games, keeps going into the previous one at half weight.
    That is not a nicety: in September the current season has no completed
    games at all, and a context layer with no data would silently do nothing
    at exactly the moment it is asked its first question.
    """
    out: list[tuple[str, int, float]] = []
    for w in range(week - 1, 0, -1):
        if w in EXCLUDE_WEEKS:
            continue
        out.append((str(season), w, 1.0))
        if len(out) >= want:
            return out

    prev = str(int(season) - 1)
    for w in range(17, 0, -1):
        if w in EXCLUDE_WEEKS:
            continue
        out.append((prev, w, 0.5))
        if len(out) >= want:
            break
    return out


# ---- scored views ------------------------------------------------------

def scored_week(
    season: str, week: int, settings: LeagueSettings, actual: bool
) -> dict[str, float]:
    """player_id -> fantasy points under THIS league's scoring, for one week."""
    rows = weekly_stats(season, week) if actual else weekly_projections(season, week)
    stype = scoring_mod.classify(settings.scoring)
    out: dict[str, float] = {}
    for row in rows or []:
        pid = str(row.get("player_id") or "")
        stats = row.get("stats") or {}
        if not pid or not stats:
            continue
        out[pid] = scoring_mod.score_player(stats, settings.scoring, stype, WEEK_BASELINE)
    return out


def usage_week(season: str, week: int) -> dict[str, dict[str, float]]:
    """player_id -> raw usage counters for one week, plus team totals.

    Snap share and target share are the numbers that move before the points
    do: a back who quietly took 70% of the snaps last week is a different
    asset than his box score says.
    """
    rows = weekly_stats(season, week) or []
    team_tgt: dict[str, float] = {}
    team_rush: dict[str, float] = {}
    for row in rows:
        team = row.get("team") or (row.get("player") or {}).get("team")
        s = row.get("stats") or {}
        if not team:
            continue
        team_tgt[team] = team_tgt.get(team, 0.0) + float(s.get("rec_tgt") or 0.0)
        team_rush[team] = team_rush.get(team, 0.0) + float(s.get("rush_att") or 0.0)

    out: dict[str, dict[str, float]] = {}
    for row in rows:
        pid = str(row.get("player_id") or "")
        s = row.get("stats") or {}
        if not pid or not s:
            continue
        team = row.get("team") or (row.get("player") or {}).get("team")
        snaps, tm_snaps = float(s.get("off_snp") or 0.0), float(s.get("tm_off_snp") or 0.0)
        if snaps <= 0:
            continue  # did not play; absence is an injury question, not a role one
        out[pid] = {
            "snap_share": snaps / tm_snaps if tm_snaps > 0 else 0.0,
            "targets": float(s.get("rec_tgt") or 0.0),
            "target_share": (
                float(s.get("rec_tgt") or 0.0) / team_tgt[team]
                if team and team_tgt.get(team) else 0.0
            ),
            "carries": float(s.get("rush_att") or 0.0),
            "carry_share": (
                float(s.get("rush_att") or 0.0) / team_rush[team]
                if team and team_rush.get(team) else 0.0
            ),
        }
    return out


# ---- the weekly player pool -------------------------------------------

def build_week_pool(
    settings: LeagueSettings, week: int, season: str | None = None
) -> dict[str, Player]:
    """Every player, projected for ONE week under this league's scoring.

    `proj_points` is the week's projection rather than the season's, so the
    draft engine's lineup optimizer works on it unchanged.
    """
    season = season or current_season()
    meta_all = all_players()
    byes = bye_weeks(season)
    opp = opponents(season).get(week, {})
    stype = scoring_mod.classify(settings.scoring)

    pool: dict[str, Player] = {}
    for row in weekly_projections(season, week) or []:
        pid = str(row.get("player_id") or "")
        meta = meta_all.get(pid) or {}
        rp = row.get("player") or {}
        pos = meta.get("position") or rp.get("position")
        if pos not in SKILL_POSITIONS:
            continue

        stats = row.get("stats") or {}
        team = row.get("team") or rp.get("team") or meta.get("team")
        name = meta.get("full_name") or " ".join(
            filter(None, [rp.get("first_name"), rp.get("last_name")])
        )
        matchup = opp.get(team) if team else None

        pool[pid] = Player(
            player_id=pid,
            name=(name or "").strip(),
            position=pos,
            team=team,
            bye_week=byes.get(team) if team else None,
            age=meta.get("age"),
            years_exp=meta.get("years_exp") or rp.get("years_exp"),
            # The weekly feed carries a fresher injury tag than the daily blob.
            injury_status=rp.get("injury_status") or meta.get("injury_status"),
            depth_chart_order=meta.get("depth_chart_order"),
            yahoo_id=str(meta["yahoo_id"]) if meta.get("yahoo_id") else None,
            proj_points=scoring_mod.score_player(stats, settings.scoring, stype, WEEK_BASELINE),
            opponent=matchup[0] if matchup else None,
            home=matchup[1] if matchup else None,
            stats={k: v for k, v in stats.items() if not k.startswith("adp")},
        )

    _rank(pool)
    return pool


def horizon_points(
    settings: LeagueSettings, week: int, weeks: int = 4, season: str | None = None
) -> dict[str, float]:
    """Average projection per game over the next `weeks` weeks.

    Waiver decisions are about the rest of the season, not Sunday. Preseason
    season-long totals are stale by October, so this re-reads the forward
    weekly projections instead, which also prices in schedule.

    A bye inside the window is left out of the average, not counted as a
    zero. Every player has exactly one, so over a season it is a wash; counted
    as a zero it only punished whoever's bye happened to fall in the next four
    weeks — it priced Evan McPherson at 4.9 against 6.6 in the games he plays,
    and put a kicker who had outscored every free agent on the drop list. A
    missed game for any other reason (injury, a demotion) still counts as zero:
    that value really is lost.
    """
    season = season or current_season()
    stype = scoring_mod.classify(settings.scoring)
    totals: dict[str, float] = {}
    counted: list[int] = []
    for w in range(week, min(week + weeks, 18)):
        try:
            rows = weekly_projections(season, w)
        except Exception:
            continue
        counted.append(w)
        for row in rows or []:
            pid = str(row.get("player_id") or "")
            stats = row.get("stats") or {}
            if not pid or not stats:
                continue
            totals[pid] = totals.get(pid, 0.0) + scoring_mod.score_player(
                stats, settings.scoring, stype, WEEK_BASELINE
            )
    if not counted:
        return {}
    byes = bye_weeks(season)
    meta = all_players()
    out: dict[str, float] = {}
    for pid, v in totals.items():
        team = (meta.get(pid) or {}).get("team")
        games = len(counted) - (1 if byes.get(team) in counted else 0)
        out[pid] = round(v / games, 2) if games else 0.0
    return out


def _rank(pool: dict[str, Player]) -> None:
    by_pos: dict[str, list[Player]] = {}
    for p in pool.values():
        by_pos.setdefault(p.position, []).append(p)
    for players in by_pos.values():
        players.sort(key=lambda p: -p.proj_points)
        for i, p in enumerate(players, 1):
            p.pos_rank = i


def ensure_players(
    pool: dict[str, Player], ids, week: int, season: str | None = None
) -> None:
    """Add zero-projection entries for rostered players the weekly feed omits.

    A player on bye, or one buried far enough down a depth chart that Sleeper
    does not project him, still occupies a roster spot and still has to appear
    in the report — silently dropping him would quietly shrink the roster.
    """
    season = season or current_season()
    missing = [str(p) for p in ids if str(p) and str(p) not in pool]
    if not missing:
        return
    meta_all = all_players()
    byes = bye_weeks(season)
    opp = opponents(season).get(week, {})
    for pid in missing:
        meta = meta_all.get(pid)
        if not meta:
            continue
        team = meta.get("team")
        matchup = opp.get(team) if team else None
        name = meta.get("full_name") or " ".join(
            filter(None, [meta.get("first_name"), meta.get("last_name")])
        )
        pool[pid] = Player(
            player_id=pid,
            name=(name or pid).strip(),
            position=meta.get("position") or "",
            team=team,
            bye_week=byes.get(team) if team else None,
            injury_status=meta.get("injury_status"),
            depth_chart_order=meta.get("depth_chart_order"),
            proj_points=0.0,
            opponent=matchup[0] if matchup else None,
            home=matchup[1] if matchup else None,
        )
