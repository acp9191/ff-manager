"""Tests for the optional written summary.

The interesting property is not that it produces prose — it is that it refuses
to produce prose containing a number nobody computed, and that it never takes
the report down when it fails.
"""

from __future__ import annotations

import json

import pytest

from ffm import brief


# ---- the guard that makes it safe to read ------------------------------

SOURCE = json.dumps({
    "your_projected_points": 118.7,
    "recommended_changes": [
        {"start": "Nico Collins", "points_gained": 7.8,
         "reasons": ["HOU has been overperforming its projections by 21%"]}
    ],
})


def test_quoting_a_given_number_is_fine():
    assert brief.invented_numbers("You gain 7.8 points.", SOURCE) == []


def test_printing_a_figure_to_fewer_decimals_is_fair_paraphrase():
    assert brief.invented_numbers("You gain 7.8 points.",
                                  json.dumps({"gain": 7.82})) == []


def test_whole_number_rounding_is_not_allowed():
    """Too loose: it let an invented '26.5 swing' pass off a real 26."""
    assert brief.invented_numbers("You project 119 points.", SOURCE) == ["119"]


def test_a_number_nobody_computed_is_caught():
    assert brief.invented_numbers(
        "Collins is averaging 14.2 targets a game.", SOURCE
    ) == ["14.2"]


def test_a_derived_total_is_caught():
    """The classic failure: quietly doing arithmetic and presenting it as fact."""
    assert brief.invented_numbers("That is a 26.5 point swing.", SOURCE) == ["26.5"]


def test_prose_without_numbers_always_passes():
    assert brief.invented_numbers("Start Collins; the matchup is soft.", SOURCE) == []


def test_percentages_resolve_against_the_source():
    assert brief.invented_numbers("Houston is beating projections by 21%.", SOURCE) == []


# ---- the payload -------------------------------------------------------

class FakeP:
    def __init__(self, name, pos="WR"):
        self.name, self.position, self.player_id = name, pos, name


class FakeSwap:
    def __init__(self):
        self.start, self.sit = FakeP("Nico Collins"), FakeP("Tee Higgins")
        self.slot, self.gain = "WR", 7.8
        self.why = ["HOU has been overperforming its projections by 21%"]


class FakeAlert:
    def __init__(self, text, level="urgent"):
        self.text, self.level, self.player_id = text, level, None


class FakePlan:
    week = 11
    recommended_points = 118.7
    followed_points = 118.7
    current_points = 108.4
    gain = 10.3
    opponent = None
    opponent_points = 0.0
    stale_context = False

    def __init__(self):
        self.alerts = [FakeAlert("James Cook is Out and in your RB")]
        self.swaps = [FakeSwap()]
        self.roster = type("R", (), {"owner_name": "You"})()


def test_the_payload_carries_only_computed_facts():
    p = brief.payload_for(FakePlan())
    assert p["week"] == 11
    assert p["your_projected_points"] == 118.7
    assert p["urgent_problems"] == ["James Cook is Out and in your RB"]
    assert p["recommended_changes"][0]["start"] == "Nico Collins"
    assert p["recommended_changes"][0]["points_gained"] == 7.8
    # No raw player pool, projections or stat lines to speculate from.
    assert "players" not in p and "pool" not in p


def test_a_preseason_payload_says_the_context_is_weak():
    plan = FakePlan()
    plan.stale_context = True
    assert "last season" in brief.payload_for(plan)["note"]


# ---- calling out ------------------------------------------------------

class Resp:
    def __init__(self, payload, status=200):
        self._p, self.status = payload, status

    def raise_for_status(self):
        if self.status >= 400:
            raise brief.requests.HTTPError("boom")

    def json(self):
        return self._p


def reply(text):
    return Resp({"choices": [{"message": {"content": text}}]})


@pytest.fixture
def keyed(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.setattr(brief.cache, "CACHE_DIR", tmp_path)
    return monkeypatch


def test_no_key_means_no_call_and_no_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(brief.requests, "post", lambda *a, **k: called.append(1))
    assert brief.summarize({"a": 1}) is None
    assert not called


def test_a_clean_summary_comes_back(keyed):
    keyed.setattr(brief.requests, "post",
                  lambda *a, **k: reply("Start Collins. It is worth 7.8 points."))
    out = brief.summarize({"points_gained": 7.8})
    assert out is not None
    assert "Collins" in out.text
    assert out.model == brief.DEFAULT_MODEL


def test_a_summary_that_invents_a_number_is_discarded(keyed):
    keyed.setattr(brief.requests, "post",
                  lambda *a, **k: reply("Collins sees 11.4 targets a game."))
    assert brief.summarize({"points_gained": 7.8}) is None


def test_the_request_is_shaped_the_way_openrouter_expects(keyed):
    seen = {}

    def capture(url, headers=None, json=None, **kw):
        seen.update(url=url, headers=headers, body=json)
        return reply("Fine.")

    keyed.setattr(brief.requests, "post", capture)
    brief.summarize({"a": 1}, model="anthropic/claude-sonnet-5")
    assert seen["url"].endswith("/chat/completions")
    assert seen["headers"]["Authorization"] == "Bearer sk-test"
    assert seen["body"]["model"] == "anthropic/claude-sonnet-5"
    assert [m["role"] for m in seen["body"]["messages"]] == ["system", "user"]


def test_an_identical_payload_is_not_paid_for_twice(keyed):
    """The watcher polls every 30 minutes; the week usually has not changed."""
    calls = []

    def once(*a, **k):
        calls.append(1)
        return reply("Start Collins.")

    keyed.setattr(brief.requests, "post", once)
    first = brief.summarize({"a": 1})
    second = brief.summarize({"a": 1})
    assert len(calls) == 1
    assert first.cached is False and second.cached is True
    assert first.text == second.text


def test_a_changed_week_is_summarized_again(keyed):
    calls = []
    keyed.setattr(brief.requests, "post",
                  lambda *a, **k: (calls.append(1), reply("Start Collins."))[1])
    brief.summarize({"a": 1})
    brief.summarize({"a": 2})
    assert len(calls) == 2


def test_a_network_failure_is_not_fatal(keyed):
    def boom(*a, **k):
        raise brief.requests.ConnectionError("down")

    keyed.setattr(brief.requests, "post", boom)
    assert brief.summarize({"a": 1}) is None


def test_an_http_error_is_not_fatal(keyed):
    keyed.setattr(brief.requests, "post", lambda *a, **k: Resp({}, status=500))
    assert brief.summarize({"a": 1}) is None


def test_a_malformed_response_is_not_fatal(keyed):
    keyed.setattr(brief.requests, "post", lambda *a, **k: Resp({"nope": True}))
    assert brief.summarize({"a": 1}) is None


def test_an_empty_completion_is_not_fatal(keyed):
    keyed.setattr(brief.requests, "post", lambda *a, **k: reply("   "))
    assert brief.summarize({"a": 1}) is None


# ---- the vocabulary guard ---------------------------------------------
# Numbers alone are not enough: the payload is dense with figures, so an
# invented claim can borrow one by coincidence.

def test_a_stat_the_input_never_mentions_is_caught():
    src = json.dumps({"reasons": ["rising role (51% -> 84% of snaps)"]})
    assert brief.unsupported_terms("He is averaging 11.4 targets a game.", src) \
        == ["targets"]


def test_vocabulary_present_in_the_input_is_allowed():
    src = json.dumps({"reasons": ["rising role (51% -> 84% of snaps)"]})
    assert brief.unsupported_terms("His snap share climbed.", src) == []


def test_ordinary_prose_is_not_flagged():
    src = json.dumps({"a": 1})
    assert brief.unsupported_terms("Start Collins; the matchup is soft.", src) == []


def test_a_borrowed_number_with_invented_vocabulary_is_still_rejected(keyed):
    """The exact case numbers alone missed."""
    keyed.setattr(
        brief.requests, "post",
        lambda *a, **k: reply("Collins is averaging 11.4 targets a game."),
    )
    # 11.4 is in the payload — as something else entirely.
    assert brief.summarize({"points_per_week": 11.4}) is None
