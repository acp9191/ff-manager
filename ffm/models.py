"""Platform-neutral domain models.

Everything downstream of the platform adapters speaks these types, so the
engine never learns whether a draft came from Sleeper or Yahoo. The in-season
features reuse Player / LeagueSettings unchanged and add Roster / LeagueWeek.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Positions we actually value. IDP and offensive line are ignored.
SKILL_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")

# Which real positions may fill each roster slot.
FLEX_ELIGIBILITY: dict[str, tuple[str, ...]] = {
    "QB": ("QB",),
    "RB": ("RB",),
    "WR": ("WR",),
    "TE": ("TE",),
    "K": ("K",),
    "DEF": ("DEF",),
    "FLEX": ("RB", "WR", "TE"),
    "WRRB_FLEX": ("RB", "WR"),
    "REC_FLEX": ("WR", "TE"),
    "SUPER_FLEX": ("QB", "RB", "WR", "TE"),
}

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def normalize_name(name: str) -> str:
    """Fold a display name to a match key: 'A.J. Brown Jr.' -> 'ajbrown'."""
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    n = n.lower().replace("&", "and")
    n = re.sub(r"[^a-z0-9\s]", "", n)
    parts = [p for p in n.split() if p not in _SUFFIXES]
    return "".join(parts)


@dataclass
class Player:
    """A draftable player with projections attached."""

    player_id: str  # Sleeper id — canonical across the app
    name: str
    position: str
    team: str | None = None
    bye_week: int | None = None
    age: float | None = None
    years_exp: int | None = None
    injury_status: str | None = None
    depth_chart_order: int | None = None
    yahoo_id: str | None = None
    espn_id: str | None = None

    proj_points: float = 0.0  # points under THIS league's scoring (season, or one week)
    adp: float = 999.0
    stats: dict[str, float] = field(default_factory=dict)

    # In-season only: who this player faces in the week being analyzed.
    opponent: str | None = None
    home: bool | None = None

    # Filled in by the engine.
    vor: float = 0.0
    tier: int = 0
    pos_rank: int = 0

    @property
    def match_key(self) -> str:
        return normalize_name(self.name)

    @property
    def is_injured(self) -> bool:
        return self.injury_status in ("Out", "IR", "PUP", "Doubtful", "Sus")

    @property
    def label(self) -> str:
        t = self.team or "FA"
        return f"{self.name} ({self.position}-{t})"


@dataclass
class LeagueSettings:
    """Normalized league configuration."""

    platform: str
    league_id: str
    draft_id: str
    name: str
    teams: int
    rounds: int
    roster_slots: list[str]  # starters + bench, e.g. ["QB","RB","RB","FLEX","BN"]
    scoring: dict[str, float] = field(default_factory=dict)
    scoring_type: str = "half_ppr"  # ppr | half_ppr | std | custom
    draft_type: str = "snake"  # snake | linear | auction
    reversal_round: int = 0  # Sleeper 3rd-round-reversal support

    @property
    def starter_slots(self) -> list[str]:
        return [s for s in self.roster_slots if s not in ("BN", "IR", "TAXI")]

    @property
    def bench_size(self) -> int:
        return sum(1 for s in self.roster_slots if s == "BN")

    @property
    def is_superflex(self) -> bool:
        return "SUPER_FLEX" in self.roster_slots

    def slot_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for s in self.starter_slots:
            counts[s] = counts.get(s, 0) + 1
        return counts


@dataclass
class DraftPick:
    """One completed selection."""

    pick_no: int  # 1-based overall
    round: int
    slot: int  # 1-based draft slot / column
    player_id: str | None
    picked_by: str | None = None


@dataclass
class DraftState:
    """A point-in-time snapshot of the live draft."""

    settings: LeagueSettings
    picks: list[DraftPick]
    my_slot: int
    status: str = "drafting"  # pre_draft | drafting | complete
    slot_names: dict[int, str] = field(default_factory=dict)

    @property
    def picks_made(self) -> int:
        return len(self.picks)

    @property
    def current_pick_no(self) -> int:
        return self.picks_made + 1

    @property
    def is_complete(self) -> bool:
        return self.status == "complete" or self.picks_made >= self.settings.teams * self.settings.rounds

    @property
    def has_started(self) -> bool:
        """Sleeper reports pre_draft until the room actually opens.

        Without this, the slot-1 manager is 'on the clock' the moment the tool
        is opened — yellow panel, terminal bell — hours before the draft.
        """
        return self.status not in ("pre_draft", "paused") or self.picks_made > 0

    def drafted_ids(self) -> set[str]:
        return {p.player_id for p in self.picks if p.player_id}

    def slot_for_pick(self, pick_no: int) -> int:
        """Snake/linear math, including Sleeper's 3rd-round reversal."""
        teams = self.settings.teams
        rnd = (pick_no - 1) // teams + 1
        idx = (pick_no - 1) % teams  # 0-based position within the round

        if self.settings.draft_type == "linear":
            return idx + 1

        reversed_round = rnd % 2 == 0
        rr = self.settings.reversal_round
        if rr and rnd >= rr:
            # From the reversal round on, the snake direction flips.
            reversed_round = not reversed_round
        return teams - idx if reversed_round else idx + 1

    def on_the_clock_slot(self) -> int | None:
        if self.is_complete or not self.has_started:
            return None
        return self.slot_for_pick(self.current_pick_no)

    def is_my_turn(self) -> bool:
        return self.on_the_clock_slot() == self.my_slot

    def my_pick_numbers(self, limit: int = 6) -> list[int]:
        """Overall pick numbers still owned by me, soonest first."""
        total = self.settings.teams * self.settings.rounds
        out = []
        for n in range(self.current_pick_no, total + 1):
            if self.slot_for_pick(n) == self.my_slot:
                out.append(n)
                if len(out) >= limit:
                    break
        return out

    def picks_by_slot(self, slot: int) -> list[DraftPick]:
        return [p for p in self.picks if p.slot == slot]

    def round_and_pick(self, pick_no: int) -> tuple[int, int]:
        teams = self.settings.teams
        return (pick_no - 1) // teams + 1, (pick_no - 1) % teams + 1


@dataclass
class Roster:
    """One team in an in-season league."""

    roster_id: int
    owner_id: str | None
    owner_name: str
    player_ids: list[str] = field(default_factory=list)
    starters: list[str] = field(default_factory=list)  # lineup as currently set
    reserve: list[str] = field(default_factory=list)   # IR
    wins: int = 0
    losses: int = 0
    ties: int = 0

    @property
    def record(self) -> str:
        base = f"{self.wins}-{self.losses}"
        return f"{base}-{self.ties}" if self.ties else base

    @property
    def bench_ids(self) -> list[str]:
        starting = set(self.starters)
        return [p for p in self.player_ids if p not in starting]


@dataclass
class LeagueWeek:
    """A point-in-time snapshot of an in-season league."""

    settings: LeagueSettings
    season: str
    week: int
    rosters: list[Roster]
    my_roster_id: int

    @property
    def me(self) -> Roster:
        for r in self.rosters:
            if r.roster_id == self.my_roster_id:
                return r
        raise KeyError(f"roster {self.my_roster_id} not in league")

    def rostered_ids(self) -> set[str]:
        """Every player owned by anyone — the complement is the waiver pool."""
        out: set[str] = set()
        for r in self.rosters:
            out.update(r.player_ids)
            out.update(r.reserve)
        return out
