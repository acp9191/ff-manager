"""Sleeper in-season league: rosters, lineups, opponents and the waiver pool.

The draft adapter reads a draft room; this reads a league that is already
playing. Same public API, same canonical player ids, so everything downstream
— scoring, the lineup optimizer, replacement levels — is reused untouched.
"""

from __future__ import annotations

import requests

from ffm.data import scoring as scoring_mod
from ffm.models import LeagueSettings, LeagueWeek, Roster
from ffm.platforms.base import AdapterError
from ffm.platforms.sleeper import user_id

BASE = "https://api.sleeper.app/v1"
TIMEOUT = 15


def _get(path: str):
    r = requests.get(f"{BASE}{path}", timeout=TIMEOUT)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def list_leagues(username: str, season: str) -> list[dict]:
    """Every league this user is in for a season."""
    uid = user_id(username)
    return _get(f"/user/{uid}/leagues/nfl/{season}") or []


class SleeperLeague:
    """One league, read at a point in the season."""

    platform = "sleeper"

    def __init__(
        self,
        league_id: str,
        username: str | None = None,
        roster_id: int | None = None,
    ):
        self.league_id = league_id
        self.username = username
        self._roster_id = roster_id
        self._league: dict | None = None
        self._settings: LeagueSettings | None = None
        self._names: dict[str, str] = {}

    # ---- config -------------------------------------------------------

    def _league_obj(self) -> dict:
        if self._league is None:
            lg = _get(f"/league/{self.league_id}")
            if not lg:
                raise AdapterError(
                    f"Sleeper league '{self.league_id}' not found. "
                    "Run `ffm leagues --user <your_sleeper_username>` to list yours."
                )
            self._league = lg
        return self._league

    def settings(self) -> LeagueSettings:
        if self._settings:
            return self._settings
        lg = self._league_obj()
        roster_slots = list(lg.get("roster_positions") or [])
        scoring = {
            k: float(v) for k, v in (lg.get("scoring_settings") or {}).items()
            if isinstance(v, (int, float))
        }
        if not scoring:
            scoring = scoring_mod.default_scoring("half_ppr")
        self._settings = LeagueSettings(
            platform="sleeper",
            league_id=str(self.league_id),
            draft_id=str(lg.get("draft_id") or ""),
            name=lg.get("name") or "Sleeper league",
            teams=int(lg.get("total_rosters") or 12),
            rounds=len(roster_slots) or 15,
            roster_slots=roster_slots,
            scoring=scoring,
            scoring_type=scoring_mod.classify(scoring),
        )
        return self._settings

    @property
    def season(self) -> str:
        return str(self._league_obj().get("season") or "")

    def _display_names(self) -> dict[str, str]:
        if not self._names:
            for u in _get(f"/league/{self.league_id}/users") or []:
                team = (u.get("metadata") or {}).get("team_name")
                self._names[u["user_id"]] = (
                    team or u.get("display_name") or u.get("username") or "?"
                )
        return self._names

    # ---- state --------------------------------------------------------

    def rosters(self) -> list[Roster]:
        names = self._display_names()
        out: list[Roster] = []
        for r in _get(f"/league/{self.league_id}/rosters") or []:
            s = r.get("settings") or {}
            owner = r.get("owner_id")
            # Sleeper pads unfilled lineup slots with "0"; they are not players.
            starters = [str(p) for p in (r.get("starters") or []) if p and str(p) != "0"]
            out.append(
                Roster(
                    roster_id=int(r.get("roster_id")),
                    owner_id=owner,
                    owner_name=names.get(owner or "", f"Team {r.get('roster_id')}"),
                    player_ids=[str(p) for p in (r.get("players") or [])],
                    starters=starters,
                    reserve=[str(p) for p in (r.get("reserve") or []) if p],
                    wins=int(s.get("wins") or 0),
                    losses=int(s.get("losses") or 0),
                    ties=int(s.get("ties") or 0),
                )
            )
        if not out:
            raise AdapterError(f"League '{self.league_id}' has no rosters yet.")
        return out

    def my_roster_id(self, rosters: list[Roster]) -> int:
        if self._roster_id:
            return self._roster_id
        if not self.username:
            raise AdapterError(
                "Pass --user <your_sleeper_username> so I know which team is yours "
                "(or --roster-id N)."
            )
        uid = user_id(self.username)
        for r in rosters:
            if r.owner_id == uid:
                self._roster_id = r.roster_id
                return r.roster_id
        raise AdapterError(
            f"'{self.username}' does not own a team in league {self.league_id}."
        )

    def week_state(self, week: int) -> LeagueWeek:
        rosters = self.rosters()
        return LeagueWeek(
            settings=self.settings(),
            season=self.season,
            week=week,
            rosters=rosters,
            my_roster_id=self.my_roster_id(rosters),
        )

    def opponent_roster_id(self, week: int, my_roster_id: int) -> int | None:
        """Who I play this week, via the shared matchup_id."""
        rows = _get(f"/league/{self.league_id}/matchups/{week}") or []
        mine = next((m for m in rows if int(m.get("roster_id", -1)) == my_roster_id), None)
        if not mine or mine.get("matchup_id") is None:
            return None
        for m in rows:
            if (
                m.get("matchup_id") == mine["matchup_id"]
                and int(m.get("roster_id", -1)) != my_roster_id
            ):
                return int(m["roster_id"])
        return None
