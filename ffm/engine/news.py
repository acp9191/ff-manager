"""What changed since the last look.

The rest of the app answers "what is true now". This answers "what just
became true", which is the question a watcher actually needs — and the two
come apart in the way that matters most. When a starting back goes down on
Friday, every backward-looking signal in this codebase is blind to it: his
backup's snap share is still last week's, his projection has not been redrawn,
and his box score is ordinary. The one thing that has changed is the depth
chart, and that is knowable immediately.

Three detectors, each diffing against the previous poll:

* **Injuries** — any status that got worse, urgent when it is your player;
  and one of your own players coming back from a sidelining tag, which
  changes who you can start.
* **Promotions** — the free agent standing directly behind an injured starter
  on his own team's depth chart. This is the pickup that wins you the week,
  and it is available before any statistical signal exists.
* **Market** — a sharp jump in how many leagues are adding someone. It is a
  crowd reacting to news the projections have not absorbed, which makes it a
  usable proxy for a beat report without parsing one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ffm.models import LeagueWeek, Player

# Worse is higher. A move up this ladder is news; a move down is recovery,
# which is news only for your own players coming back from a sidelining tag.
SEVERITY = {
    None: 0, "": 0, "Probable": 0,
    "Questionable": 1, "Doubtful": 2,
    "Out": 3, "Sus": 3, "PUP": 4, "IR": 4,
}

# Statuses that actually vacate a role.
SIDELINED = ("Out", "Doubtful", "IR", "PUP", "Sus")

# A promotion is only interesting if the man going down was really playing.
STARTER_DEPTH = 2
MIN_INJURED_VALUE = 6.0

# How much of a jump in league-wide adds counts as news breaking.
MARKET_SPIKE = 8000

MAX_PROMOTIONS = 6
MAX_MARKET = 4


@dataclass
class NewsItem:
    kind: str          # injury | promotion | market
    urgent: bool
    headline: str
    detail: list[str] = field(default_factory=list)
    key: str = ""
    player_id: str = ""
    # "Pick him up" is half an answer; this is the other half — whether to
    # bother, and who goes. None when no advisor was supplied.
    move: object | None = None


@dataclass
class Snapshot:
    """The world as of the previous poll. Empty on the first one."""

    injuries: dict[str, str] = field(default_factory=dict)
    market: dict[str, int] = field(default_factory=dict)
    # What each player was projected for while he was still healthy. Measuring
    # a vacated role by the injured man's *current* projection is circular —
    # ruling him out collapses it to zero, so the job looks worthless at
    # exactly the moment it comes free. This is the number from before.
    values: dict[str, float] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.injuries and not self.market

    def worth_before(self, pid: str, fallback: float = 0.0) -> float:
        return self.values.get(pid, fallback)

    def to_json(self) -> dict:
        return {"injuries": self.injuries, "market": self.market,
                "values": self.values}

    @classmethod
    def from_json(cls, raw: dict | None) -> "Snapshot":
        raw = raw or {}
        return cls(
            injuries={str(k): str(v) for k, v in (raw.get("injuries") or {}).items()},
            market={str(k): int(v) for k, v in (raw.get("market") or {}).items()},
            values={str(k): float(v) for k, v in (raw.get("values") or {}).items()},
        )


def take_snapshot(
    pool: dict[str, Player], market: dict[str, int], prev: Snapshot | None = None
) -> Snapshot:
    """Only players with a status or a real projection are stored."""
    prev = prev or Snapshot()
    values: dict[str, float] = {}
    for pid, p in pool.items():
        if p.injury_status in SIDELINED:
            # Freeze the pre-injury number rather than overwriting it with the
            # zero he carries now; it is the only record of the job he held.
            keep = prev.values.get(pid)
            if keep is not None:
                values[pid] = keep
                continue
        if p.proj_points >= 1.0:
            values[pid] = round(p.proj_points, 2)
    return Snapshot(
        injuries={pid: p.injury_status for pid, p in pool.items() if p.injury_status},
        market=dict(market),
        values=values,
    )


def _worsened(before: str | None, after: str | None) -> bool:
    return SEVERITY.get(after or "", 0) > SEVERITY.get(before or "", 0)


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _recovered(before: str | None, after: str | None) -> bool:
    """Back from a tag that kept him off the field."""
    return before in SIDELINED and SEVERITY.get(after or "", 0) < SEVERITY[before]


def injury_changes(
    pool: dict[str, Player], prev: Snapshot, mine: set[str], week: int
) -> list[NewsItem]:
    """Statuses that got worse since the last poll, and your players' returns.

    Two filters, both learned from a first run that produced thirty-four
    notifications: without a previous poll there is no such thing as a change,
    and a Questionable tag on a player you do not own is not news — half the
    league carries one by Friday. Someone else's player only matters once he
    is actually sidelined, because that is when a job opens up.
    """
    if prev.empty:
        return []
    out: list[NewsItem] = []
    for pid, p in pool.items():
        before = prev.injuries.get(pid)
        is_mine = pid in mine
        # Week 4: Evans and Coker were both cleared from Out and you heard
        # about neither, because a move down the ladder was treated as noise.
        # For someone else's player it is; for yours it reopens a lineup spot.
        if is_mine and _recovered(before, p.injury_status):
            now = p.injury_status or "active"
            out.append(NewsItem(
                kind="injury",
                urgent=False,
                headline=f"{p.name} ({p.position}-{p.team or 'FA'}) is now "
                         f"{now} (was {before})",
                detail=["He is on your roster and available to start again."
                        if not p.injury_status else
                        f"He is on your roster. {now} is a game-time call — "
                        f"check inactives before starting him."],
                key=f"injury:{week}:{_today()}:{pid}:{before}>{now}",
                player_id=pid,
            ))
            continue
        if not _worsened(before, p.injury_status):
            continue
        if not is_mine and (
            p.injury_status not in SIDELINED or p.proj_points < MIN_INJURED_VALUE
        ):
            continue
        was = f" (was {before})" if before else ""
        out.append(NewsItem(
            kind="injury",
            urgent=is_mine,
            headline=f"{p.name} ({p.position}-{p.team or 'FA'}) is now "
                     f"{p.injury_status}{was}",
            detail=["He is on your roster." if is_mine else "Not on your roster."],
            # Keyed on the transition and the day, not the status alone:
            # Questionable on Wednesday, Out, back to Questionable on Sunday,
            # then Out again that night is two rulings-out, and the second is
            # the one that matters.
            key=f"injury:{week}:{_today()}:{pid}:{before or 'active'}>{p.injury_status}",
            player_id=pid,
        ))
    return out


def _next_man_up(
    injured: Player, pool: dict[str, Player], owned: set[str]
) -> Player | None:
    """The free agent standing behind an injured player on his own depth chart."""
    if not injured.team:
        return None
    behind = [
        p for p in pool.values()
        if p.team == injured.team
        and p.position == injured.position
        and p.player_id != injured.player_id
        and p.player_id not in owned
        and p.injury_status not in SIDELINED
    ]
    if not behind:
        return None
    ranked = [p for p in behind if p.depth_chart_order]
    if ranked:
        order = injured.depth_chart_order or 0
        after = [p for p in ranked if p.depth_chart_order > order]
        pick = min(after or ranked, key=lambda p: p.depth_chart_order)
        return pick
    # No depth chart for this room; fall back to whoever projects best.
    return max(behind, key=lambda p: p.proj_points)


def promotions(
    league: LeagueWeek, pool: dict[str, Player], prev: Snapshot, week: int,
    advisor=None,
) -> list[NewsItem]:
    """Free agents whose job just got bigger because someone ahead went down."""
    owned = league.rostered_ids()
    mine = set(league.me.player_ids)
    out: list[NewsItem] = []

    for pid, p in pool.items():
        if p.injury_status not in SIDELINED:
            continue
        # What the job was worth before he lost it. Falls back to his current
        # projection only when there is no history to consult.
        job_worth = prev.worth_before(pid, p.proj_points)
        # A depth-chart starter who projects for nothing is not an opportunity,
        # whatever his listed rank: a third-string QB going on IR promotes
        # somebody into a role that scores nothing.
        if pid not in mine and job_worth < MIN_INJURED_VALUE:
            continue
        # Only report it when the news is new, not every poll for the rest of
        # the season. A player already sidelined last poll is old news.
        if not _worsened(prev.injuries.get(pid), p.injury_status) and not prev.empty:
            continue

        heir = _next_man_up(p, pool, owned)
        if heir is None:
            continue
        detail = [
            f"{p.name} is {p.injury_status}; {heir.name} is next on the "
            f"{p.team} depth chart and is a free agent in your league.",
        ]
        if heir.proj_points > 0:
            detail.append(f"Projected {heir.proj_points:.1f} this week.")
        if pid in mine:
            detail.append("You own the injured player — this is his handcuff.")
        # Speculative by construction: his projection has not been redrawn to
        # reflect a job he inherited hours ago.
        move = (
            advisor.evaluate(heir, speculative=True, vacated=p,
                             vacated_value=job_worth)
            if advisor else None
        )
        out.append(NewsItem(
            kind="promotion",
            urgent=pid in mine,
            headline=f"Pick up {heir.name} ({heir.position}-{heir.team}) — "
                     f"{p.name} is {p.injury_status}",
            detail=detail,
            key=f"promotion:{week}:{heir.player_id}:{pid}",
            player_id=heir.player_id,
            move=move,
        ))

    out.sort(key=lambda i: (not i.urgent, -pool[i.player_id].proj_points))
    return out[:MAX_PROMOTIONS]


def market_spikes(
    league: LeagueWeek, pool: dict[str, Player], market: dict[str, int],
    prev: Snapshot, week: int, advisor=None,
) -> list[NewsItem]:
    """A sudden rush of adds across Sleeper: the crowd reacting to news."""
    if prev.empty:
        return []  # no baseline yet; everything would look like a spike
    owned = league.rostered_ids()
    out: list[NewsItem] = []
    for pid, count in market.items():
        if pid in owned or pid not in pool:
            continue
        jump = count - prev.market.get(pid, 0)
        if jump < MARKET_SPIKE:
            continue
        p = pool[pid]
        out.append(NewsItem(
            kind="market",
            move=advisor.evaluate(p, speculative=True) if advisor else None,
            urgent=False,
            headline=f"{p.name} ({p.position}-{p.team or 'FA'}) is being added "
                     f"in {jump:,} more leagues",
            detail=[f"{count:,} adds in the last 48 hours, and he is a free "
                    "agent in yours."],
            key=f"market:{week}:{pid}:{count // MARKET_SPIKE}",
            player_id=pid,
        ))
    out.sort(key=lambda i: -market.get(i.player_id, 0))
    return out[:MAX_MARKET]


def scan(
    league: LeagueWeek,
    pool: dict[str, Player],
    market: dict[str, int],
    prev: Snapshot,
    advisor=None,
) -> tuple[list[NewsItem], Snapshot]:
    """All three detectors, plus the snapshot to diff against next time.

    `advisor` is a roster_move.MoveAdvisor. Optional so the detectors stay
    testable on their own, but the watcher always supplies one — an alert
    telling you to add somebody without saying who goes is the thing this
    whole module exists to avoid.
    """
    week = league.week
    mine = set(league.me.player_ids)
    items: list[NewsItem] = []
    items += injury_changes(pool, prev, mine, week)
    items += promotions(league, pool, prev, week, advisor)
    items += market_spikes(league, pool, market, prev, week, advisor)
    items.sort(key=lambda i: (not i.urgent, i.kind))
    return items, take_snapshot(pool, market, prev)
