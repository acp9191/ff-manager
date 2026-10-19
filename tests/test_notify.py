"""Tests for delivery: what gets said, and — mostly — what does not get said twice."""

from __future__ import annotations

import pytest

from ffm import notify, watch
from ffm.notify import Message
from ffm.watch import WatchState, choose, due_digests, parse_digest_times


class Recorder(notify.Notifier):
    name = "recorder"

    def __init__(self):
        self.sent: list[Message] = []

    def send(self, msg):
        self.sent.append(msg)
        return True


def msg(key, title="t"):
    return Message(title=title, lines=["x"], key=key)


def state(tmp_path, week=3, sent=None):
    return WatchState(path=tmp_path / "w.json", week=week, sent=dict(sent or {}))


def digest_factory():
    return Message(title="digest", lines=["everything"])


# ---- the dedupe rule ---------------------------------------------------

def test_the_first_pass_of_a_week_sends_one_digest_not_a_flood(tmp_path):
    st = state(tmp_path, sent={})
    out = choose([msg("a"), msg("b"), msg("c")], st, digest_factory, 3)
    assert [m.title for m in out] == ["digest"]
    # Everything it saw is now recorded, so none of it is news next time.
    assert all(st.seen(k) for k in ("a", "b", "c"))


def test_nothing_is_ever_sent_twice(tmp_path):
    st = state(tmp_path, sent={"a": 1.0})
    assert choose([msg("a")], st, digest_factory, 3) == []


def test_only_genuinely_new_messages_go_out(tmp_path):
    st = state(tmp_path, sent={"a": 1.0})
    out = choose([msg("a"), msg("b")], st, digest_factory, 3)
    assert [m.key for m in out] == ["b"]


def test_a_large_burst_collapses_into_a_digest(tmp_path):
    st = state(tmp_path, sent={"seed": 1.0})
    pending = [msg(f"k{i}") for i in range(watch.MAX_BURST + 1)]
    out = choose(pending, st, digest_factory, 3)
    assert [m.title for m in out] == ["digest"]
    assert all(st.seen(f"k{i}") for i in range(watch.MAX_BURST + 1))


def test_a_small_burst_is_sent_individually(tmp_path):
    st = state(tmp_path, sent={"seed": 1.0})
    pending = [msg(f"k{i}") for i in range(watch.MAX_BURST)]
    out = choose(pending, st, digest_factory, 3)
    assert len(out) == watch.MAX_BURST


# ---- state persistence -------------------------------------------------

def test_state_survives_a_restart(tmp_path):
    st = WatchState(path=tmp_path / "w.json", week=3)
    st.mark("a")
    st.save()
    assert WatchState.load_from(tmp_path / "w.json", 3).seen("a")


def test_a_new_week_starts_from_a_clean_slate(tmp_path):
    """Last week's alerts must not silence this week's."""
    st = WatchState(path=tmp_path / "w.json", week=3)
    st.mark("a")
    st.save()
    assert not WatchState.load_from(tmp_path / "w.json", 4).seen("a")


def test_corrupt_state_does_not_take_the_watcher_down(tmp_path):
    p = tmp_path / "w.json"
    p.write_text("{not json")
    assert WatchState.load_from(p, 3).sent == {}


# ---- digest scheduling -------------------------------------------------

def test_digest_times_parse():
    assert parse_digest_times("09:00,17:30") == [(9, 0), (17, 30)]
    assert parse_digest_times("") == []


def test_a_bad_digest_time_is_rejected_clearly():
    with pytest.raises(ValueError, match="digest-at"):
        parse_digest_times("9am")


def test_a_digest_fires_once_the_time_has_passed():
    from datetime import datetime
    now = datetime(2025, 11, 9, 9, 30)
    assert due_digests([(9, 0)], now) == ["digest:2025-11-09:09:00"]
    assert due_digests([(17, 0)], now) == []


def test_a_long_asleep_machine_does_not_dump_the_whole_day_at_once():
    """Opening the laptop at midnight must not replay every digest."""
    from datetime import datetime
    assert due_digests([(9, 0), (17, 30)], datetime(2025, 11, 9, 23, 55)) == []


def test_a_digest_is_still_sent_after_a_late_start():
    from datetime import datetime
    assert due_digests([(9, 0)], datetime(2025, 11, 9, 11, 0)) == [
        "digest:2025-11-09:09:00"
    ]


def test_digest_keys_are_dated_so_tomorrow_sends_again():
    from datetime import datetime
    a = due_digests([(9, 0)], datetime(2025, 11, 9, 10, 0))
    b = due_digests([(9, 0)], datetime(2025, 11, 10, 10, 0))
    assert a != b


# ---- message construction ---------------------------------------------

class FakePlayer:
    def __init__(self, name, pos="RB", team="NYJ", opponent="NE", home=False):
        self.name, self.position, self.team = name, pos, team
        self.opponent, self.home = opponent, home
        self.player_id = name


class FakeAlert:
    def __init__(self, text, level="urgent", player_id=None):
        self.text, self.level, self.player_id = text, level, player_id


class FakeSwap:
    def __init__(self, start, sit, gain=4.2, slot="RB", why=None):
        self.start, self.sit, self.gain, self.slot = start, sit, gain, slot
        self.why = why or ["soft matchup vs NE"]


class FakePlan:
    week = 3
    stale_context = False
    current_points = 100.0
    recommended_points = 110.0
    opponent = None
    opponent_points = 0.0

    def __init__(self, alerts, swaps):
        self.alerts, self.swaps = alerts, swaps
        self.roster = type("R", (), {"owner_name": "You"})()


def test_an_alert_carries_its_fix_so_you_need_not_open_a_laptop():
    hall, spears = FakePlayer("Breece Hall"), FakePlayer("Tyjae Spears")
    plan = FakePlan(
        [FakeAlert("Breece Hall is Out and in your RB2", player_id="Breece Hall")],
        [FakeSwap(spears, hall)],
    )
    out = notify.alert_messages(plan)
    assert len(out) == 1 and out[0].urgent
    body = out[0].plain()
    assert "Breece Hall is Out" in body
    assert "Start Tyjae Spears" in body
    assert "+4.2" in body
    assert "in your RB" in body


def test_the_fix_names_the_slot_when_the_replacement_changes_position():
    """A back going down can slide a FLEX up and pull a receiver in behind it."""
    hall = FakePlayer("Breece Hall", pos="RB")
    addison = FakePlayer("Jordan Addison", pos="WR", team="MIN")
    plan = FakePlan(
        [FakeAlert("Breece Hall is Out and in your RB", player_id="Breece Hall")],
        [FakeSwap(addison, hall, gain=9.9, slot="FLEX")],
    )
    body = notify.alert_messages(plan)[0].plain()
    assert "in your FLEX" in body
    assert "instead" not in body


def test_non_urgent_alerts_do_not_page_you():
    plan = FakePlan([FakeAlert("Questionable — check inactives", "warn")], [])
    assert notify.alert_messages(plan) == []


def test_swap_messages_are_keyed_by_the_pair_so_a_stable_call_repeats_once():
    a, b = FakePlayer("A"), FakePlayer("B")
    plan = FakePlan([], [FakeSwap(a, b)])
    first = notify.swap_messages(plan)[0]
    assert first.key == notify.swap_messages(plan)[0].key
    assert "A" in first.key and "B" in first.key


def test_html_is_escaped_so_a_name_cannot_break_the_message():
    assert notify._esc("A & B <c>") == "A &amp; B &lt;c&gt;"


# ---- notifier selection -----------------------------------------------

def test_telegram_wins_when_configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("NTFY_TOPIC", "x")
    assert notify.from_env().name == "telegram"


def test_ntfy_is_used_when_there_is_no_bot(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("NTFY_TOPIC", "x")
    assert notify.from_env().name == "ntfy"


def test_an_explicit_choice_overrides_detection(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("FFM_NOTIFY", "console")
    assert notify.from_env().name == "console"


def test_nothing_configured_falls_back_to_console(monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC",
              "DISCORD_WEBHOOK_URL", "FFM_NOTIFY"):
        monkeypatch.delenv(k, raising=False)
    assert notify.from_env().name == "console"


def test_telegram_posts_the_expected_payload(monkeypatch):
    seen = {}

    class Resp:
        def raise_for_status(self): pass

    def fake_post(url, json=None, **kw):
        seen["url"], seen["json"] = url, json
        return Resp()

    monkeypatch.setattr(notify.requests, "post", fake_post)
    notify.TelegramNotifier("TOK", "42").send(Message("Title", ["a & b"]))
    assert seen["url"].endswith("/botTOK/sendMessage")
    assert seen["json"]["chat_id"] == "42"
    assert "<b>Title</b>" in seen["json"]["text"]
    assert "a &amp; b" in seen["json"]["text"]


# ---- the heartbeat -----------------------------------------------------
# "The container is up" says nothing about a poller: a wedged one is still a
# running process. The healthcheck reads this instead.

def test_a_completed_pass_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(watch.cache, "CACHE_DIR", tmp_path)
    watch.beat()
    age = watch.last_beat()
    assert age is not None and age < 5


def test_no_heartbeat_reads_as_unknown_rather_than_healthy(tmp_path, monkeypatch):
    monkeypatch.setattr(watch.cache, "CACHE_DIR", tmp_path)
    assert watch.last_beat() is None


def test_a_corrupt_heartbeat_is_not_treated_as_recent(tmp_path, monkeypatch):
    monkeypatch.setattr(watch.cache, "CACHE_DIR", tmp_path)
    (tmp_path / watch.HEARTBEAT).write_text("not a timestamp")
    assert watch.last_beat() is None


def test_a_stale_heartbeat_reports_its_true_age(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(watch.cache, "CACHE_DIR", tmp_path)
    (tmp_path / watch.HEARTBEAT).write_text(str(int(time.time()) - 7200))
    assert watch.last_beat() > 7000


def test_an_unwritable_cache_dir_does_not_kill_the_watcher(tmp_path, monkeypatch):
    """A degraded healthcheck must never take down the thing it is checking."""
    monkeypatch.setattr(watch.cache, "CACHE_DIR", tmp_path / "file" / "nested")
    (tmp_path / "file").write_text("I am a file, not a directory")
    watch.beat()  # must not raise


# ---- what is allowed to buzz a phone -----------------------------------
# A pickup you would not actually make is not news. Everything filtered here
# still appears on the waiver board and in the digest.

class FakeMove:
    def __init__(self, verdict, summary="v"):
        self.verdict, self.summary = verdict, summary
        self.lines, self.open_spots, self.drop = ["because"], 0, None

    @property
    def do_it(self):
        return self.verdict in ("add", "worth_it", "inherit")

    actionable = property(lambda self: self.do_it)


class FakePickup:
    def __init__(self, name, verdict, kind="breakout"):
        self.player = FakePlayer(name, pos="RB")
        self.kind, self.why, self.move = kind, ["why"], FakeMove(verdict)
        self.horizon_points, self.horizon_gain, self.drop = 5.0, 0.0, None


class FakeBoard:
    week, horizon_weeks = 3, 4

    def __init__(self, pickups):
        self.pickups = pickups


def test_a_flyer_does_not_reach_your_phone():
    board = FakeBoard([FakePickup("Flyer", "speculative")])
    assert notify.waiver_messages(board) == []


@pytest.mark.parametrize("verdict", ["add", "worth_it", "inherit"])
def test_a_real_pickup_does(verdict):
    board = FakeBoard([FakePickup("Real", verdict)])
    assert len(notify.waiver_messages(board)) == 1


@pytest.mark.parametrize("verdict", ["marginal", "skip", "hold"])
def test_nor_does_anything_you_would_not_do(verdict):
    assert notify.waiver_messages(FakeBoard([FakePickup("No", verdict)])) == []


def test_the_filter_can_be_opted_out_of():
    board = FakeBoard([FakePickup("Flyer", "speculative")])
    assert len(notify.waiver_messages(board, speculative=True)) == 1


class FakeNews:
    def __init__(self, kind, verdict=None, urgent=False):
        self.kind, self.urgent = kind, urgent
        self.headline, self.detail, self.key = "h", ["d"], f"{kind}:1"
        self.move = FakeMove(verdict) if verdict else None


def test_an_injury_to_your_own_player_is_never_filtered():
    """Not a suggestion to gamble — it is your lineup breaking."""
    out = notify.news_messages([FakeNews("injury", urgent=True)])
    assert len(out) == 1


def test_a_market_spike_on_a_flyer_stays_quiet():
    assert notify.news_messages([FakeNews("market", "speculative")]) == []


def test_a_promotion_into_a_real_job_still_gets_through():
    """The most valuable alert must survive the filter."""
    out = notify.news_messages([FakeNews("promotion", "inherit")])
    assert len(out) == 1


def test_a_promotion_into_nothing_does_not():
    assert notify.news_messages([FakeNews("promotion", "speculative")]) == []
