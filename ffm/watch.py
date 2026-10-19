"""The long-running half: poll the league, notify when something changes.

The hard part of a watcher is not polling, it is silence. A process that
re-sends "your RB2 is Out" every thirty minutes gets muted within a day, and a
muted alert is worse than no alert because you believe you are covered. So
every message carries a dedupe key and nothing is ever sent twice.

The first pass of a week is treated specially. Priming a fresh state file by
firing off every swap and every waiver target at once would be exactly the
flood the dedupe exists to prevent, so the first pass sends one digest and
records everything it saw as already delivered. From then on you only hear
about what actually changed.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ffm import brief, cache, notify
from ffm.data import weekly
from ffm.engine import news
from ffm.notify import Message, Notifier
from ffm.season import build_season

# More than this in one pass means something structural changed (a bye week
# landing, a fresh roster); send the digest instead of a burst of alerts.
MAX_BURST = 4

# Written after every successful pass. A poller that has wedged is still a
# running process, so "the container is up" says nothing useful — this is what
# the Docker healthcheck actually reads.
HEARTBEAT = "watch_heartbeat"


def beat() -> None:
    path = cache.CACHE_DIR / HEARTBEAT
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(int(time.time())))
    except OSError:
        pass  # a missing heartbeat degrades the healthcheck, never the watcher


def last_beat() -> float | None:
    """Seconds since the last successful pass, or None if it never ran."""
    try:
        return time.time() - int((cache.CACHE_DIR / HEARTBEAT).read_text().strip())
    except (OSError, ValueError):
        return None


@dataclass
class WatchState:
    """What has already been sent, so it is not sent again."""

    path: Path
    week: int
    sent: dict[str, float] = field(default_factory=dict)
    # The world as of the last poll. Unlike `sent`, this survives a week
    # rollover: an injury that happened on Sunday is not new again on Tuesday.
    snapshot: news.Snapshot = field(default_factory=news.Snapshot)

    @classmethod
    def load(cls, league_id: str, week: int) -> "WatchState":
        return cls.load_from(cache.CACHE_DIR / f"watch_{league_id}.json", week)

    @classmethod
    def load_from(cls, path: Path, week: int) -> "WatchState":
        """State is scoped to a week, so last week's alerts never silence this one."""
        sent: dict[str, float] = {}
        snapshot = news.Snapshot()
        try:
            raw = json.loads(path.read_text())
            if int(raw.get("week", -1)) == week:
                sent = dict(raw.get("sent") or {})
            snapshot = news.Snapshot.from_json(raw.get("snapshot"))
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        return cls(path=path, week=week, sent=sent, snapshot=snapshot)

    @property
    def primed(self) -> bool:
        return bool(self.sent)

    def seen(self, key: str) -> bool:
        return key in self.sent

    def mark(self, key: str) -> None:
        self.sent[key] = time.time()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "week": self.week,
            "sent": self.sent,
            "snapshot": self.snapshot.to_json(),
        }))
        tmp.replace(self.path)


def parse_digest_times(spec: str) -> list[tuple[int, int]]:
    """'09:00,17:30' -> [(9, 0), (17, 30)]."""
    out: list[tuple[int, int]] = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            hh, mm = chunk.split(":")
            out.append((int(hh), int(mm)))
        except ValueError:
            raise ValueError(f"bad --digest-at time {chunk!r}; expected HH:MM")
    return out


# How late a digest may be sent. A laptop asleep since morning should still
# get the pre-kickoff summary when it wakes, but a machine opened at midnight
# should not receive every digest the day had scheduled.
DIGEST_GRACE_MINUTES = 240


def due_digests(
    times: list[tuple[int, int]], now: datetime, grace: int = DIGEST_GRACE_MINUTES
) -> list[str]:
    """Digest keys whose moment has passed today and is still recent enough."""
    minutes_now = now.hour * 60 + now.minute
    out = []
    for hh, mm in times:
        late = minutes_now - (hh * 60 + mm)
        if 0 <= late <= grace:
            out.append(f"digest:{now:%Y-%m-%d}:{hh:02d}:{mm:02d}")
    return out


def collect(plan, board, speculative: bool = False) -> list[Message]:
    """Everything worth saying about the current state, most urgent first."""
    msgs = notify.alert_messages(plan)
    msgs += notify.swap_messages(plan)
    if board:
        msgs += notify.waiver_messages(board, speculative=speculative)
    return msgs


def choose(pending: list[Message], state: WatchState, digest, week: int) -> list[Message]:
    """Decide what actually gets sent, and record it as delivered.

    Split out from the polling so the rule that matters — that you are told
    each thing exactly once — can be tested without a league or a network.
    """
    fresh = [m for m in pending if not state.seen(m.key)]

    if not state.primed:
        # First look at this week: one digest, and everything else recorded as
        # already known, so the next pass reports changes rather than history.
        msg = digest()
        msg.key = f"digest:first:{week}"
        for m in pending:
            state.mark(m.key)
        state.mark(msg.key)
        return [msg]

    if not fresh:
        return []

    if len(fresh) > MAX_BURST:
        # Something structural moved — a bye landing, a roster rebuilt. A burst
        # of separate alerts is the flood the dedupe exists to prevent.
        msg = digest()
        msg.key = f"digest:burst:{week}:{int(time.time())}"
        for m in fresh:
            state.mark(m.key)
        state.mark(msg.key)
        return [msg]

    for m in fresh:
        state.mark(m.key)
    return fresh


def one_pass(
    league_id: str,
    notifier: Notifier,
    *,
    username: str | None = None,
    roster_id: int | None = None,
    week: int | None = None,
    horizon: int = 4,
    lookback: int = 5,
    opinions_path: Path | None = None,
    digest_times: list[tuple[int, int]] | None = None,
    now: datetime | None = None,
    skip_waivers: bool = False,
    want_brief: bool = False,
    speculative: bool = False,
) -> list[Message]:
    """Check once and send whatever is new. Returns what was sent."""
    sess = build_season(
        league_id, username=username, roster_id=roster_id, week=week,
        lookback=lookback, horizon_weeks=horizon, opinions_path=opinions_path,
    )
    plan = sess.plan()
    board = None if skip_waivers else sess.board(limit=8)

    state = WatchState.load(league_id, sess.week)
    now = now or datetime.now()

    market = weekly.trending("add", hours=48, limit=120)
    items, snapshot = news.scan(
        sess.state, sess.pool, market, state.snapshot, sess.advisor()
    )
    pending = (collect(plan, board, speculative)
               + notify.news_messages(items, speculative))

    def digest():
        # Only the digest gets a written summary. An urgent alert has to be
        # instant and unambiguous, which is not a job for a paragraph.
        written = brief.for_report(plan, board) if want_brief else None
        return notify.digest_message(plan, board, brief=written)

    sent = choose(pending, state, digest, sess.week)
    for key in due_digests(digest_times or [], now):
        if not state.seen(key):
            msg = digest()
            msg.key = key
            state.mark(key)
            sent.append(msg)

    for msg in sent:
        notifier.send(msg)

    state.snapshot = snapshot
    state.save()
    return sent


def watch(
    league_id: str,
    notifier: Notifier,
    console,
    *,
    interval: float = 1800.0,
    once: bool = False,
    freshness: int | None = None,
    **kw,
) -> None:
    """Poll until interrupted, sending only what is new."""
    # Cached data older than the poll interval would make every diff empty.
    weekly.set_freshness(int(freshness if freshness is not None else interval / 2))
    errors = 0
    from ffm import __version__, build_id

    console.print(
        f"[green]Watching[/green] league {league_id} · notifying via "
        f"[bold]{notifier.describe()}[/bold] · every {interval / 60:g} min · "
        f"Ctrl-C to stop"
    )
    # Printed every start so a stale image is visible in `docker compose logs`
    # rather than only in the absence of a feature.
    console.print(f"[dim]ff-manager {__version__} build {build_id()}[/dim]")
    while True:
        try:
            sent = one_pass(league_id, notifier, **kw)
            errors = 0
            beat()
            stamp = time.strftime("%H:%M:%S")
            if sent:
                console.print(f"[dim]{stamp}[/dim] sent {len(sent)}: "
                              + "; ".join(m.title for m in sent))
            else:
                console.print(f"[dim]{stamp} nothing new[/dim]")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            # A watcher that dies on a network blip is not a watcher.
            errors += 1
            console.print(f"[red]check failed ({errors}): {exc}[/red]")
            if errors >= 10:
                raise
            time.sleep(min(300, interval * errors / 4))
            continue
        if once:
            return
        time.sleep(interval)
