"""Sleeper adapter. Fully public API — no auth, no rate limit worth worrying about."""

from __future__ import annotations

import requests

from ffm.data import scoring as scoring_mod
from ffm.models import DraftPick, DraftState, LeagueSettings, Player
from ffm.platforms.base import AdapterError, DraftAdapter

BASE = "https://api.sleeper.app/v1"
TIMEOUT = 15

# Sleeper's draft-settings keys -> our roster slot names.
_SLOT_KEYS = {
    "slots_qb": "QB", "slots_rb": "RB", "slots_wr": "WR", "slots_te": "TE",
    "slots_k": "K", "slots_def": "DEF", "slots_flex": "FLEX",
    "slots_super_flex": "SUPER_FLEX", "slots_wr_rb_flex": "WRRB_FLEX",
    "slots_wr_te_flex": "REC_FLEX", "slots_bn": "BN",
}


def _get(path: str):
    r = requests.get(f"{BASE}{path}", timeout=TIMEOUT)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def user_id(username: str) -> str:
    u = _get(f"/user/{username}")
    if not u or not u.get("user_id"):
        raise AdapterError(f"Sleeper user '{username}' not found.")
    return u["user_id"]


def list_drafts(username: str, season: str) -> list[dict]:
    """Every draft across the user's leagues this season, plus their mocks."""
    uid = user_id(username)
    out: list[dict] = []
    seen: set[str] = set()

    for d in _get(f"/user/{uid}/drafts/nfl/{season}") or []:
        if d.get("draft_id") not in seen:
            seen.add(d["draft_id"])
            out.append(d)

    for lg in _get(f"/user/{uid}/leagues/nfl/{season}") or []:
        for d in _get(f"/league/{lg['league_id']}/drafts") or []:
            if d.get("draft_id") not in seen:
                seen.add(d["draft_id"])
                d.setdefault("metadata", {})["name"] = lg.get("name")
                out.append(d)

    order = {"drafting": 0, "paused": 1, "pre_draft": 2, "complete": 3}
    out.sort(key=lambda d: (order.get(d.get("status"), 4), -(d.get("start_time") or 0)))
    return out


class SleeperAdapter(DraftAdapter):
    platform = "sleeper"

    def __init__(self, draft_id: str, username: str | None = None, my_slot: int | None = None):
        self.draft_id = draft_id
        self.username = username
        self._my_slot = my_slot
        self._draft: dict | None = None
        self._settings: LeagueSettings | None = None
        self._slot_names: dict[int, str] = {}
        self._user_id: str | None = None
        self._league_id: str | None = None
        # An explicit --slot is authoritative; an auto-resolved one is not
        # trusted until the draft actually starts, because commissioners
        # commonly have Sleeper randomize the order at launch.
        self._slot_locked = my_slot is not None

    # ---- config -------------------------------------------------------

    def _draft_obj(self, refresh: bool = False) -> dict:
        if self._draft is None or refresh:
            d = _get(f"/draft/{self.draft_id}")
            if not d:
                raise AdapterError(f"Sleeper draft '{self.draft_id}' not found.")
            self._draft = d
        return self._draft

    def league_settings(self) -> LeagueSettings:
        if self._settings:
            return self._settings

        d = self._draft_obj()
        ds = d.get("settings") or {}
        league_id = d.get("league_id")

        roster_slots: list[str] = []
        scoring: dict[str, float] = {}
        name = (d.get("metadata") or {}).get("name") or "Sleeper draft"

        league = _get(f"/league/{league_id}") if league_id else None
        if league:
            roster_slots = list(league.get("roster_positions") or [])
            scoring = {k: float(v) for k, v in (league.get("scoring_settings") or {}).items()
                       if isinstance(v, (int, float))}
            name = league.get("name") or name

        if not roster_slots:
            # Mock drafts have no league; the slot counts live on the draft.
            for key, slot in _SLOT_KEYS.items():
                roster_slots.extend([slot] * int(ds.get(key) or 0))
        if not scoring:
            stype = (d.get("metadata") or {}).get("scoring_type", "half_ppr")
            stype = {"2qb": "half_ppr", "dynasty": "half_ppr"}.get(stype, stype)
            scoring = scoring_mod.default_scoring(
                stype if stype in ("ppr", "half_ppr", "std") else "half_ppr"
            )

        teams = int(ds.get("teams") or d.get("settings", {}).get("teams") or 12)
        rounds = int(ds.get("rounds") or (len(roster_slots) or 15))

        self._settings = LeagueSettings(
            platform="sleeper",
            league_id=str(league_id or ""),
            draft_id=self.draft_id,
            name=name,
            teams=teams,
            rounds=rounds,
            roster_slots=roster_slots,
            scoring=scoring,
            scoring_type=scoring_mod.classify(scoring),
            draft_type="linear" if d.get("type") == "linear" else "snake",
            reversal_round=int(ds.get("reversal_round") or 0),
        )
        self._load_slot_names(d, league_id)
        return self._settings

    def _uid(self) -> str | None:
        if self._user_id is None and self.username:
            try:
                self._user_id = user_id(self.username)
            except AdapterError:
                self._user_id = ""
        return self._user_id or None

    def _load_slot_names(self, draft: dict, league_id: str | None) -> None:
        order = draft.get("draft_order") or {}  # user_id -> slot
        names: dict[str, str] = {}
        if league_id:
            for u in _get(f"/league/{league_id}/users") or []:
                names[u["user_id"]] = u.get("display_name") or u.get("username") or "?"
        for uid, slot in order.items():
            self._slot_names[int(slot)] = names.get(uid, f"Team {slot}")
        self._league_id = league_id

    def my_slot(self) -> int:
        if self._my_slot:
            return self._my_slot
        order = self._draft_obj().get("draft_order") or {}
        uid = self._uid()
        if uid and uid in order:
            self._my_slot = int(order[uid])
            return self._my_slot
        raise AdapterError(
            "Sleeper has not published the draft order yet (it appears when the "
            "draft starts, or when the commissioner sets it). Pass --slot N to "
            "start now — 1 is the first pick."
        )

    def _refresh_order(self, draft: dict) -> None:
        """Keep the draft order in sync until it is final.

        Two things force this to run on every poll rather than once at startup:
        the board is normally opened while draft_order is still null, and a
        randomized order can be reshuffled at the moment the draft launches. So
        an auto-resolved slot stays provisional through pre_draft and is only
        locked once picks are actually being made.
        """
        order = draft.get("draft_order") or {}
        if not order:
            return

        started = (draft.get("status") or "") != "pre_draft"
        if self._slot_locked and started:
            return

        self._load_slot_names(draft, self._league_id or draft.get("league_id"))
        uid = self._uid()
        if uid and uid in order:
            self._my_slot = int(order[uid])
            if started:
                self._slot_locked = True

    # ---- live state ---------------------------------------------------

    def fetch_state(self) -> DraftState:
        settings = self.league_settings()
        raw = _get(f"/draft/{self.draft_id}/picks") or []
        picks = [
            DraftPick(
                pick_no=int(p["pick_no"]),
                round=int(p.get("round") or 1),
                slot=int(p.get("draft_slot") or 0),
                player_id=str(p["player_id"]) if p.get("player_id") else None,
                picked_by=p.get("picked_by"),
            )
            for p in raw
        ]
        picks.sort(key=lambda p: p.pick_no)

        draft = self._draft_obj(refresh=True)
        self._refresh_order(draft)
        status = draft.get("status") or "drafting"
        return DraftState(
            settings=settings,
            picks=picks,
            my_slot=self.my_slot(),
            status=status,
            slot_names=dict(self._slot_names),
        )

    def resolve_player(self, raw_id: str, pool: dict[str, Player]) -> str | None:
        return raw_id if raw_id in pool else None
