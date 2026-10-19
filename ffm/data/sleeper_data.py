"""Sleeper's public data endpoints: the player universe, projections and ADP.

None of this needs auth, so it powers the value model for Yahoo drafts too —
Sleeper carries a `yahoo_id` on each player, giving a clean join key.
"""

from __future__ import annotations

import requests

from ffm import cache
from ffm.data import scoring as scoring_mod
from ffm.models import SKILL_POSITIONS, LeagueSettings, Player

BASE = "https://api.sleeper.app"
TIMEOUT = 30

PLAYERS_TTL = 24 * 3600
PROJ_TTL = 3 * 3600
SCHEDULE_TTL = 7 * 24 * 3600


def _get(url: str, **params):
    r = requests.get(url, params=params or None, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def nfl_state() -> dict:
    return cache.cached("nfl_state", 3600, lambda: _get(f"{BASE}/v1/state/nfl"))


def current_season() -> str:
    try:
        return str(nfl_state().get("season") or "")
    except Exception:
        import datetime
        return str(datetime.date.today().year)


def all_players() -> dict[str, dict]:
    """The full Sleeper player DB, trimmed to skill positions to keep it small."""

    def fetch() -> dict[str, dict]:
        raw = _get(f"{BASE}/v1/players/nfl")
        keep = (
            "player_id", "full_name", "first_name", "last_name", "position",
            "fantasy_positions", "team", "age", "years_exp", "injury_status",
            "depth_chart_order", "search_rank", "yahoo_id", "espn_id", "status", "active",
        )
        out = {}
        for pid, p in raw.items():
            if p.get("position") not in SKILL_POSITIONS:
                continue
            out[pid] = {k: p.get(k) for k in keep}
        return out

    return cache.cached("players_nfl", PLAYERS_TTL, fetch)


def season_projections(season: str | None = None) -> list[dict]:
    season = season or current_season()

    def fetch() -> list[dict]:
        return _get(
            f"{BASE}/projections/nfl/{season}",
            season_type="regular",
            **{"position[]": list(SKILL_POSITIONS)},
        )

    # requests encodes a list param as repeated keys, which is what Sleeper wants.
    return cache.cached(f"projections_{season}", PROJ_TTL, fetch)


def bye_weeks(season: str | None = None) -> dict[str, int]:
    """Derive byes from the schedule: the week a team has no game."""
    season = season or current_season()

    def fetch() -> dict[str, int]:
        games = _get(f"{BASE}/schedule/nfl/regular/{season}")
        playing: dict[int, set[str]] = {}
        teams: set[str] = set()
        for g in games:
            wk = g.get("week")
            if not wk or wk > 18:
                continue
            for side in ("home", "away"):
                t = g.get(side)
                if t:
                    playing.setdefault(wk, set()).add(t)
                    teams.add(t)
        byes: dict[str, int] = {}
        for wk in sorted(playing):
            # Byes only exist in weeks where some teams are idle.
            for t in teams - playing[wk]:
                byes.setdefault(t, wk)
        return byes

    try:
        return cache.cached(f"byes_{season}", SCHEDULE_TTL, fetch)
    except Exception:
        return {}


def _adp_key(settings: LeagueSettings) -> str:
    if settings.is_superflex:
        return "adp_2qb"
    base = scoring_mod.classify(settings.scoring).split("+")[0]
    return {"ppr": "adp_ppr", "half_ppr": "adp_half_ppr", "std": "adp_std"}[base]


def build_player_pool(settings: LeagueSettings, season: str | None = None) -> dict[str, Player]:
    """The draftable universe, scored and ADP-tagged for THIS league."""
    players = all_players()
    projections = season_projections(season)
    byes = bye_weeks(season)
    adp_key = _adp_key(settings)
    scoring_type = scoring_mod.classify(settings.scoring)

    pool: dict[str, Player] = {}
    for row in projections:
        pid = str(row.get("player_id") or "")
        meta = players.get(pid)
        if not meta:
            continue
        pos = meta.get("position")
        if pos not in SKILL_POSITIONS:
            continue

        stats = row.get("stats") or {}
        adp = stats.get(adp_key)
        # 999 is Sleeper's "unranked" sentinel.
        adp = float(adp) if adp and float(adp) < 999 else 999.0

        name = meta.get("full_name") or " ".join(
            filter(None, [meta.get("first_name"), meta.get("last_name")])
        )
        team = meta.get("team")
        pool[pid] = Player(
            player_id=pid,
            name=name.strip(),
            position=pos,
            team=team,
            bye_week=byes.get(team) if team else None,
            age=meta.get("age"),
            years_exp=meta.get("years_exp"),
            injury_status=meta.get("injury_status"),
            depth_chart_order=meta.get("depth_chart_order"),
            yahoo_id=str(meta["yahoo_id"]) if meta.get("yahoo_id") else None,
            espn_id=str(meta["espn_id"]) if meta.get("espn_id") else None,
            proj_points=scoring_mod.score_player(stats, settings.scoring, scoring_type),
            adp=adp,
            stats={k: v for k, v in stats.items() if not k.startswith("adp")},
        )

    _assign_pos_ranks(pool)
    return pool


def _assign_pos_ranks(pool: dict[str, Player]) -> None:
    by_pos: dict[str, list[Player]] = {}
    for p in pool.values():
        by_pos.setdefault(p.position, []).append(p)
    for players in by_pos.values():
        players.sort(key=lambda p: -p.proj_points)
        for i, p in enumerate(players, 1):
            p.pos_rank = i


def index_by_yahoo_id(pool: dict[str, Player]) -> dict[str, Player]:
    return {p.yahoo_id: p for p in pool.values() if p.yahoo_id}


def index_by_name(pool: dict[str, Player]) -> dict[tuple[str, str], Player]:
    """(match_key, position) -> Player, for name-based fallback matching."""
    idx: dict[tuple[str, str], Player] = {}
    for p in pool.values():
        idx.setdefault((p.match_key, p.position), p)
    return idx
