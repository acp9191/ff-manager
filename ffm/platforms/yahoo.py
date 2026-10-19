"""Yahoo Fantasy adapter (OAuth2 + the famously nested Yahoo JSON).

Two deliberate robustness choices:

* Yahoo's JSON nests everything under numeric string keys whose depth changes
  between endpoints, so instead of indexing fixed paths we walk the tree and
  collect dicts by the keys we care about.
* Yahoo's scoring settings arrive as bare stat ids, and the published id maps
  disagree with each other. Guessing would silently corrupt every projection,
  so we pull Yahoo's own stat_categories and map by stat *name*.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, Iterator

import requests

from ffm.data import scoring as scoring_mod
from ffm.models import DraftPick, DraftState, LeagueSettings, Player, normalize_name
from ffm.platforms.base import AdapterError, DraftAdapter

_NOT_AUTHORIZED = (
    "Yahoo returned 403 'not authorized' for every Fantasy endpoint.\n\n"
    "The login worked and the token is valid, so this is the app's API "
    "permissions, not your credentials. The authorize step accepts the "
    "fantasy scope as soon as an app has any permission attached, but the API "
    "itself checks that Fantasy Sports *Read* was actually granted.\n\n"
    "  1. Open https://developer.yahoo.com/apps/ and click your app\n"
    "  2. Under 'API Permissions' confirm 'Fantasy Sports' is ticked AND the\n"
    "     'Read' option beneath it is selected, then Save\n"
    "  3. Re-authorize: ffm yahoo-auth --print-url  (the old token was granted\n"
    "     under the previous permissions and will keep failing)\n\n"
    "If Fantasy Sports is already ticked with Read, delete the app and create a "
    "new one with the permission selected at creation time — adding it to an "
    "existing Yahoo app is known not to take effect."
)

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
API = "https://fantasysports.yahooapis.com/fantasy/v2"
TOKEN_PATH = Path(
    os.environ.get("FFM_YAHOO_TOKEN", Path.home() / ".config" / "ff-manager" / "yahoo_token.json")
)
OOB = "oob"

# Yahoo roster slot label -> our slot name.
SLOT_MAP = {
    "QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "K",
    "DEF": "DEF", "D/ST": "DEF", "DST": "DEF",
    "W/R/T": "FLEX", "W/R": "WRRB_FLEX", "W/T": "REC_FLEX",
    "Q/W/R/T": "SUPER_FLEX", "SUPER_FLEX": "SUPER_FLEX",
    "BN": "BN", "IR": "IR", "IR+": "IR", "NA": "IR",
}

# Yahoo stat display name -> Sleeper projection stat key.
STAT_NAME_MAP = {
    "passing yards": "pass_yd",
    "passing touchdowns": "pass_td",
    "interceptions": "pass_int",
    "passing attempts": "pass_att",
    "completions": "pass_cmp",
    "rushing attempts": "rush_att",
    "rushing yards": "rush_yd",
    "rushing touchdowns": "rush_td",
    "receptions": "rec",
    "receiving yards": "rec_yd",
    "receiving touchdowns": "rec_td",
    "targets": "rec_tgt",
    "fumbles lost": "fum_lost",
    "fumbles": "fum",
    "2-point conversions": "rush_2pt",
    "field goals 0-19 yards": "fgm_0_19",
    "field goals 20-29 yards": "fgm_20_29",
    "field goals 30-39 yards": "fgm_30_39",
    "field goals 40-49 yards": "fgm_40_49",
    "field goals 50+ yards": "fgm_50p",
    "point after attempt made": "xpm",
    "point after attempt missed": "xpmiss",
    "first downs receiving": "rec_fd",
    "first downs rushing": "rush_fd",
}


# ---- Yahoo JSON helpers ------------------------------------------------

def walk(node: Any) -> Iterator[dict]:
    """Yield every dict nested anywhere inside a Yahoo response."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def collect(node: Any, *required_keys: str) -> list[dict]:
    """Every dict in the tree containing all of `required_keys`."""
    return [d for d in walk(node) if all(k in d for k in required_keys)]


# ---- OAuth -------------------------------------------------------------

class YahooAuth:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str = OOB):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.token: dict = {}

    # -- persistence
    def load(self) -> bool:
        if TOKEN_PATH.exists():
            try:
                self.token = json.loads(TOKEN_PATH.read_text())
                return bool(self.token.get("refresh_token"))
            except (json.JSONDecodeError, OSError):
                return False
        return False

    def save(self) -> None:
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(json.dumps(self.token, indent=2))
        os.chmod(TOKEN_PATH, 0o600)

    # -- flow
    def authorize_url(self) -> str:
        q = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": "fspt-r",
                "language": "en-us",
            }
        )
        return f"{AUTH_URL}?{q}"

    @staticmethod
    def extract_code(value: str) -> str:
        """Accept either a bare code or the whole redirected URL.

        With a real redirect URI the browser lands on a page that will not
        load, and the code is only visible in the address bar — so pasting the
        entire URL is the natural thing to do.
        """
        value = value.strip().strip('"').strip("'")
        if "code=" in value:
            parsed = urllib.parse.urlparse(value)
            found = urllib.parse.parse_qs(parsed.query).get("code")
            if found:
                return found[0]
        return value

    def _basic_header(self) -> str:
        raw = f"{self.client_id}:{self.client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def exchange(self, code: str) -> None:
        code = self.extract_code(code)
        # Yahoo documents HTTP Basic for the client credentials. Passing them in
        # the form body is also accepted, but it is not the documented path, so
        # prefer Basic and keep the body form as a fallback.
        r = requests.post(
            TOKEN_URL,
            data={
                "redirect_uri": self.redirect_uri,
                "code": code,
                "grant_type": "authorization_code",
            },
            headers={
                "Authorization": self._basic_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=20,
        )
        if r.status_code != 200:
            r = requests.post(
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "redirect_uri": self.redirect_uri,
                    "code": code,
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=20,
            )
        if r.status_code != 200:
            body = r.text[:300]
            if "invalid_grant" in body or "expired" in body:
                raise AdapterError(
                    "That Yahoo verification code is expired or already used.\n\n"
                    "Yahoo codes are single-use and last about a minute, so grab a "
                    "fresh one and exchange it right away:\n"
                    "  1. ffm yahoo-auth --print-url\n"
                    "  2. open the URL, approve, copy the new code\n"
                    "  3. ffm yahoo-auth --code <new code>"
                )
            raise AdapterError(f"Yahoo token exchange failed ({r.status_code}): {body}")
        self.token = r.json()
        self.token["expires_at"] = time.time() + float(self.token.get("expires_in", 3600))
        self.save()

    def refresh(self) -> None:
        r = requests.post(
            TOKEN_URL,
            data={
                "redirect_uri": self.redirect_uri,
                "refresh_token": self.token.get("refresh_token", ""),
                "grant_type": "refresh_token",
            },
            headers={"Authorization": self._basic_header()},
            timeout=20,
        )
        if r.status_code != 200:
            raise AdapterError(
                "Yahoo token refresh failed — run `ffm yahoo-auth` again."
            )
        refresh_token = self.token.get("refresh_token")
        self.token = r.json()
        self.token.setdefault("refresh_token", refresh_token)
        self.token["expires_at"] = time.time() + float(self.token.get("expires_in", 3600))
        self.save()

    def access_token(self) -> str:
        if not self.token and not self.load():
            raise AdapterError("Not authenticated with Yahoo — run `ffm yahoo-auth`.")
        if time.time() >= float(self.token.get("expires_at", 0)) - 60:
            self.refresh()
        return self.token["access_token"]

    def preflight(self) -> str | None:
        """Check the app can request Fantasy Sports before sending the user off.

        A Yahoo app created without the Fantasy Sports permission still
        authenticates fine for other scopes, so the failure only shows up as an
        `invalid_scope` redirect after the user has already left for the
        browser. Catching it here turns a dead end into a fixable instruction.
        """
        try:
            r = requests.get(
                AUTH_URL,
                params={
                    "client_id": self.client_id,
                    "redirect_uri": self.redirect_uri,
                    "response_type": "code",
                    "scope": "fspt-r",
                },
                timeout=15,
                allow_redirects=False,
            )
        except requests.RequestException:
            return None  # offline or blocked; let the real flow report it

        location = r.headers.get("location", "")
        if "/oauth2/error" not in location:
            return None
        error = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get(
            "error", ["unknown"]
        )[0]
        if error == "invalid_scope":
            return (
                "This Yahoo app cannot request Fantasy Sports access.\n\n"
                "Your Client ID and Secret are valid — the app just has no "
                "Fantasy Sports permission attached. To fix it:\n"
                "  1. Open https://developer.yahoo.com/apps/ and click your app\n"
                "  2. Under 'API Permissions', tick 'Fantasy Sports'\n"
                "  3. Select the 'Read' checkbox that appears beneath it\n"
                "  4. Save, then run `ffm yahoo-auth` again\n\n"
                "If Fantasy Sports is not offered, the app's type is wrong: "
                "recreate it as an 'Installed Application'."
            )
        if error == "invalid_request":
            return (
                f"Yahoo rejected redirect_uri {self.redirect_uri!r} for this app.\n\n"
                "It must exactly match a Redirect URI registered on the app at "
                "https://developer.yahoo.com/apps/ , or be 'oob' if the app "
                "allows the out-of-band flow."
            )
        if error == "invalid_client":
            return (
                "Yahoo rejected the app credentials (invalid_client). Check "
                "YAHOO_CLIENT_ID and YAHOO_CLIENT_SECRET in .env match the app "
                "at https://developer.yahoo.com/apps/"
            )
        return f"Yahoo rejected the authorization request ({error})."

    def interactive_login(self, console=None) -> None:
        problem = self.preflight()
        if problem:
            raise AdapterError(problem)
        url = self.authorize_url()
        if console is not None:
            console.print("\nOpen this URL, approve access, then paste the code back here:\n")
        # Plain stdout so the URL is never wrapped and stays copyable.
        print(url, flush=True)
        try:
            webbrowser.open(url)
        except Exception:
            pass
        self.exchange(input("Yahoo verification code (or the whole redirect URL): "))


# ---- Adapter -----------------------------------------------------------

class YahooAdapter(DraftAdapter):
    platform = "yahoo"

    def __init__(self, league_key: str, auth: YahooAuth, my_slot: int | None = None):
        self.league_key = league_key
        self.auth = auth
        self._my_slot = my_slot
        self._settings: LeagueSettings | None = None
        self._team_slots: dict[str, int] = {}   # team_key -> draft slot
        self._my_team_key: str | None = None
        self._slot_names: dict[int, str] = {}

    def _get(self, path: str) -> dict:
        r = requests.get(
            f"{API}/{path}",
            headers={"Authorization": f"Bearer {self.auth.access_token()}"},
            params={"format": "json"},
            timeout=20,
        )
        if r.status_code == 401:
            self.auth.refresh()
            r = requests.get(
                f"{API}/{path}",
                headers={"Authorization": f"Bearer {self.auth.access_token()}"},
                params={"format": "json"},
                timeout=20,
            )
        if r.status_code == 403:
            raise AdapterError(_NOT_AUTHORIZED)
        if r.status_code != 200:
            raise AdapterError(f"Yahoo API {path} failed ({r.status_code}): {r.text[:200]}")
        return r.json()

    # -- scoring
    def _stat_names(self) -> dict[str, str]:
        """stat_id -> lowercase display name, straight from Yahoo."""
        data = self._get("game/nfl/stat_categories")
        out: dict[str, str] = {}
        for d in collect(data, "stat_id", "name"):
            out[str(d["stat_id"])] = str(d["name"]).strip().lower()
        return out

    def _scoring(self, settings_json: dict) -> dict[str, float]:
        names = self._stat_names()
        scoring: dict[str, float] = {}
        for d in collect(settings_json, "stat_id", "value"):
            key = STAT_NAME_MAP.get(names.get(str(d["stat_id"]), ""))
            if not key:
                continue
            try:
                scoring[key] = float(d["value"])
            except (TypeError, ValueError):
                continue
        return scoring

    def league_settings(self) -> LeagueSettings:
        if self._settings:
            return self._settings

        meta = self._get(f"league/{self.league_key}")
        info = next((d for d in walk(meta) if "league_key" in d and "num_teams" in d), {})

        sj = self._get(f"league/{self.league_key}/settings")
        slots: list[str] = []
        for d in collect(sj, "position", "count"):
            slot = SLOT_MAP.get(str(d["position"]).upper())
            if slot:
                slots.extend([slot] * int(d["count"]))
        if not slots:
            raise AdapterError("Could not read Yahoo roster positions for this league.")

        scoring = self._scoring(sj)
        if not scoring:
            scoring = scoring_mod.default_scoring("half_ppr")

        teams = int(info.get("num_teams") or 12)
        self._settings = LeagueSettings(
            platform="yahoo",
            league_id=self.league_key,
            draft_id=self.league_key,
            name=str(info.get("name") or "Yahoo league"),
            teams=teams,
            rounds=max(1, len(slots)),
            roster_slots=slots,
            scoring=scoring,
            scoring_type=scoring_mod.classify(scoring),
            draft_type="snake",
        )
        self._load_teams()
        return self._settings

    def _load_teams(self) -> None:
        data = self._get(f"league/{self.league_key}/teams")
        for d in collect(data, "team_key"):
            key = str(d["team_key"])
            if "name" in d:
                self._slot_names.setdefault(key, d["name"])  # keyed by team for now
            if str(d.get("is_owned_by_current_login", "0")) == "1":
                self._my_team_key = key
        # Draft slots come from round 1 of the draft results.
        self._team_slots = {}

    def _ensure_slots(self, results: list[dict]) -> None:
        if self._team_slots:
            return
        for d in results:
            if int(d.get("round", 0)) == 1:
                self._team_slots[str(d["team_key"])] = int(d["pick"])
        names = dict(self._slot_names)
        self._slot_names = {}
        for team_key, slot in self._team_slots.items():
            self._slot_names[slot] = names.get(team_key, f"Team {slot}")

    def my_slot(self) -> int:
        if self._my_slot:
            return self._my_slot
        if self._my_team_key and self._my_team_key in self._team_slots:
            self._my_slot = self._team_slots[self._my_team_key]
            return self._my_slot
        raise AdapterError(
            "Could not determine your Yahoo draft slot yet (needs round 1 to start). "
            "Pass --slot N to set it manually."
        )

    def fetch_state(self) -> DraftState:
        settings = self.league_settings()
        data = self._get(f"league/{self.league_key}/draftresults")
        results = [d for d in collect(data, "pick", "player_key") if d.get("player_key")]
        results.sort(key=lambda d: int(d["pick"]))
        self._ensure_slots(collect(data, "pick", "team_key"))

        picks: list[DraftPick] = []
        for d in results:
            pick_no = int(d["pick"])
            rnd = int(d.get("round") or ((pick_no - 1) // settings.teams + 1))
            team_key = str(d.get("team_key") or "")
            picks.append(
                DraftPick(
                    pick_no=pick_no,
                    round=rnd,
                    slot=self._team_slots.get(team_key, 0),
                    player_id=str(d["player_key"]),  # nfl.p.<yahoo_id>
                    picked_by=team_key,
                )
            )

        total = settings.teams * settings.rounds
        return DraftState(
            settings=settings,
            picks=picks,
            my_slot=self.my_slot(),
            status="complete" if len(picks) >= total else "drafting",
            slot_names=dict(self._slot_names),
        )

    def resolve_player(self, raw_id: str, pool: dict[str, Player]) -> str | None:
        """`nfl.p.12345` -> our Sleeper player id, via Sleeper's yahoo_id."""
        yid = raw_id.rsplit(".", 1)[-1]
        for p in pool.values():
            if p.yahoo_id == yid:
                return p.player_id
        return None


def list_leagues(auth: YahooAuth) -> list[dict]:
    """The logged-in user's NFL leagues for the current season."""
    r = requests.get(
        f"{API}/users;use_login=1/games;game_keys=nfl/leagues",
        headers={"Authorization": f"Bearer {auth.access_token()}"},
        params={"format": "json"},
        timeout=20,
    )
    if r.status_code == 403:
        raise AdapterError(_NOT_AUTHORIZED)
    if r.status_code != 200:
        raise AdapterError(f"Yahoo league list failed ({r.status_code}): {r.text[:200]}")
    out = []
    seen = set()
    for d in collect(r.json(), "league_key", "name"):
        if d["league_key"] in seen:
            continue
        seen.add(d["league_key"])
        out.append(
            {
                "league_key": d["league_key"],
                "name": d.get("name"),
                "num_teams": d.get("num_teams"),
                "season": d.get("season"),
                "draft_status": d.get("draft_status"),
            }
        )
    return out
