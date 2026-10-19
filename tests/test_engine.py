"""Tests for the load-bearing logic: draft math, lineups, survival, scoring."""

from __future__ import annotations

import random

import pytest

from ffm.data import scoring as sc
from ffm.engine import survival
from ffm.engine.lineup import optimize
from ffm.engine.recommend import position_cap
from ffm.engine.replacement import replacement_points, starter_demand
from ffm.engine.tiers import assign_tiers
from ffm.models import DraftPick, DraftState, LeagueSettings, Player, normalize_name

SLOTS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF"] + ["BN"] * 6


def league(**kw) -> LeagueSettings:
    base = dict(
        platform="test", league_id="1", draft_id="1", name="test",
        teams=12, rounds=15, roster_slots=list(SLOTS),
        scoring=sc.default_scoring("half_ppr"), scoring_type="half_ppr",
    )
    base.update(kw)
    return LeagueSettings(**base)


def player(pid, pos, pts, adp=999.0, **kw) -> Player:
    return Player(player_id=pid, name=pid, position=pos, proj_points=pts, adp=adp, **kw)


# ---- snake math --------------------------------------------------------

def test_snake_order_reverses_each_round():
    st = DraftState(settings=league(teams=10), picks=[], my_slot=3)
    assert [st.slot_for_pick(n) for n in range(1, 11)] == list(range(1, 11))
    assert [st.slot_for_pick(n) for n in range(11, 21)] == list(range(10, 0, -1))
    assert st.slot_for_pick(21) == 1


def test_linear_draft_never_reverses():
    st = DraftState(settings=league(teams=10, draft_type="linear"), picks=[], my_slot=1)
    assert st.slot_for_pick(11) == 1
    assert st.slot_for_pick(20) == 10


def test_third_round_reversal():
    st = DraftState(settings=league(teams=10, reversal_round=3), picks=[], my_slot=1)
    assert st.slot_for_pick(1) == 1            # R1 forward
    assert st.slot_for_pick(11) == 10          # R2 back
    assert st.slot_for_pick(21) == 10          # R3 repeats the turn
    assert st.slot_for_pick(31) == 1


def test_my_pick_numbers_are_mine():
    st = DraftState(settings=league(teams=12, rounds=4), picks=[], my_slot=5)
    nums = st.my_pick_numbers(limit=4)
    assert nums == [5, 20, 29, 44]
    assert all(st.slot_for_pick(n) == 5 for n in nums)


def test_my_pick_numbers_skip_completed_picks():
    picks = [DraftPick(pick_no=i, round=1, slot=i, player_id=str(i)) for i in range(1, 8)]
    st = DraftState(settings=league(teams=12, rounds=4), picks=picks, my_slot=5)
    assert st.my_pick_numbers(limit=2) == [20, 29]  # pick 5 is already gone


# ---- lineup optimizer --------------------------------------------------

def test_flex_takes_best_leftover_not_first_eligible():
    players = [player("rb1", "RB", 200), player("rb2", "RB", 190),
               player("wr1", "WR", 100), player("te1", "TE", 50)]
    lu = optimize(players, ["RB", "WR", "FLEX"])
    assert lu.points == pytest.approx(200 + 100 + 190)


def test_superflex_prefers_qb_when_qb_is_better():
    players = [player("qb1", "QB", 300), player("qb2", "QB", 280), player("rb1", "RB", 150)]
    lu = optimize(players, ["QB", "SUPER_FLEX", "RB"])
    assert lu.points == pytest.approx(300 + 280 + 150)


def test_empty_slots_are_reported():
    lu = optimize([player("rb1", "RB", 100)], ["QB", "RB"])
    assert [slot for slot, pl in lu.assignment if pl is None] == ["QB"]


def test_ineligible_players_never_fill_a_slot():
    lu = optimize([player("k1", "K", 500)], ["QB", "RB"])
    assert lu.points == 0.0


def test_optimizer_matches_brute_force():
    """The Hungarian solution must equal exhaustive search on random rosters."""
    from functools import lru_cache
    from ffm.models import FLEX_ELIGIBILITY

    def brute(players, slots):
        @lru_cache(maxsize=None)
        def rec(si, used):
            if si == len(slots):
                return 0.0
            elig = FLEX_ELIGIBILITY.get(slots[si], (slots[si],))
            best = rec(si + 1, used)
            for i, p in enumerate(players):
                if not (used >> i) & 1 and p.position in elig:
                    best = max(best, p.proj_points + rec(si + 1, used | (1 << i)))
            return best
        return round(rec(0, 0), 2)

    rng = random.Random(11)
    slotsets = [
        ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX"],
        ["QB", "RB", "WR", "SUPER_FLEX", "FLEX"],
        ["QB", "RB", "WR", "REC_FLEX", "WRRB_FLEX"],
    ]
    for _ in range(120):
        slots = rng.choice(slotsets)
        ps = [
            player(f"p{i}", rng.choice(["QB", "RB", "WR", "TE"]), round(rng.uniform(0, 300), 1))
            for i in range(rng.randint(1, 10))
        ]
        assert optimize(ps, slots).points == pytest.approx(brute(ps, slots), abs=0.02)


# ---- scoring -----------------------------------------------------------

def test_ppr_and_standard_differ_by_receptions():
    stats = {"rec": 80, "rec_yd": 1000, "rec_td": 8, "pts_ppr": 228.0, "pts_std": 148.0}
    assert sc.compute_points(stats, sc.default_scoring("ppr")) == pytest.approx(228.0)
    assert sc.compute_points(stats, sc.default_scoring("std")) == pytest.approx(148.0)


def test_te_premium_is_picked_up():
    stats = {"rec": 90, "bonus_rec_te": 90}
    scoring = dict(sc.default_scoring("half_ppr"), bonus_rec_te=0.5)
    assert sc.compute_points(stats, scoring) == pytest.approx(45 + 45)


def test_classify_detects_format():
    assert sc.classify({"rec": 1.0}) == "ppr"
    assert sc.classify({"rec": 0.5}) == "half_ppr"
    assert sc.classify({"rec": 0.0}) == "std"
    assert sc.classify({"rec": 0.5, "bonus_rec_te": 0.5}) == "half_ppr+te_prem"


def test_broken_scoring_falls_back_to_precomputed():
    """A partial settings object must not silently zero out every projection."""
    stats = {"rec": 80, "rec_yd": 1000, "pts_half_ppr": 180.0}
    assert sc.score_player(stats, {"rec": 0.5}, "half_ppr") == pytest.approx(180.0)


def test_team_defense_uses_precomputed_points():
    """DEF has no granular stat line in the feed, so it must fall through."""
    assert sc.score_player({"pts_ppr": 106.0}, sc.default_scoring("ppr"), "ppr") == 106.0


# ---- survival ----------------------------------------------------------

def test_availability_drops_as_the_turn_lengthens():
    p = player("x", "RB", 200, adp=30)
    near = survival.availability([p], 20, 24)["x"]
    far = survival.availability([p], 20, 60)["x"]
    assert near > far


def test_faller_is_not_written_off():
    """A player well past his ADP who is still on the board may last again."""
    p = player("faller", "WR", 200, adp=10)
    assert survival.availability([p], 60, 70)["faller"] > 0.2


def test_expected_best_is_bounded_by_the_best_option():
    pairs = [(100.0, 0.5), (80.0, 0.5), (60.0, 1.0)]
    assert 60.0 <= survival.expected_best(pairs) <= 100.0


def test_expected_best_of_certain_player_is_his_value():
    assert survival.expected_best([(90.0, 1.0), (50.0, 1.0)]) == pytest.approx(90.0)


# ---- replacement & caps ------------------------------------------------

def test_flex_deepens_the_baseline_of_flex_positions():
    pool = [player(f"rb{i}", "RB", 300 - i * 5) for i in range(40)]
    pool += [player(f"wr{i}", "WR", 290 - i * 3) for i in range(60)]
    pool += [player(f"qb{i}", "QB", 320 - i * 6) for i in range(30)]
    pool += [player(f"te{i}", "TE", 200 - i * 8) for i in range(25)]
    demand = starter_demand(league(), pool)
    assert demand["QB"] == 12                 # 1 per team, no flex eligibility
    assert demand["RB"] + demand["WR"] + demand["TE"] > 12 * 5  # flex adds bodies
    repl = replacement_points(league(), pool)
    assert repl["RB"] > 0 and repl["QB"] > 0


def test_position_cap_scales_with_starters():
    assert position_cap("QB", league()) == 2                       # 1 starter + 1
    assert position_cap("QB", league(roster_slots=["QB", "SUPER_FLEX"] + SLOTS)) >= 3
    assert position_cap("RB", league()) > position_cap("TE", league())


# ---- tiers -------------------------------------------------------------

def test_tiers_break_on_large_gaps():
    pool = [player("a", "RB", 300), player("b", "RB", 298), player("c", "RB", 296),
            player("d", "RB", 200), player("e", "RB", 198), player("f", "RB", 196)]
    assign_tiers(pool)
    assert pool[0].tier == pool[1].tier == pool[2].tier
    assert pool[3].tier > pool[0].tier


# ---- name matching -----------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("A.J. Brown", "AJ Brown"),
    ("Marvin Harrison Jr.", "Marvin Harrison"),
    ("Amon-Ra St. Brown", "AmonRa St Brown"),
    ("D'Andre Swift", "DAndre Swift"),
])
def test_name_normalization_matches_variants(a, b):
    assert normalize_name(a) == normalize_name(b)


def test_calibration_does_not_rescue_certain_goners():
    """An elite player must not inherit a uniform survival floor.

    Scaling probabilities by a constant used to lift everyone who was certainly
    gone to the same non-trivial survival chance, so the whole top of the board
    read identically.
    """
    elite = [player(f"e{i}", "RB", 300 - i, adp=float(i + 1)) for i in range(6)]
    filler = [player(f"f{i}", "WR", 100, adp=float(60 + i)) for i in range(80)]
    probs = survival.availability(elite + filler, 5, 20)
    assert probs["e0"] < 0.05                       # ADP 1, gone by pick 20
    assert probs["f79"] > 0.5                       # ADP 139, still there
    top = [probs[f"e{i}"] for i in range(6)]
    assert len(set(round(p, 3) for p in top)) > 1   # not all identical
    assert top == sorted(top)                       # better ADP survives less


# ---- sleeper adapter ---------------------------------------------------

def test_autoresolved_slot_follows_a_predraft_reshuffle():
    """A randomized order can change at launch; don't lock in a stale seat."""
    from ffm.platforms.sleeper import SleeperAdapter

    a = SleeperAdapter("d1", username="me")
    a._user_id = "u-me"
    a._settings = league()

    a._refresh_order({"status": "pre_draft", "draft_order": {"u-me": 9}, "league_id": None})
    assert a._my_slot == 9 and not a._slot_locked

    # Commissioner re-randomizes before the draft starts.
    a._refresh_order({"status": "pre_draft", "draft_order": {"u-me": 3}, "league_id": None})
    assert a._my_slot == 3

    # Once picks are landing the slot is final and stops moving.
    a._refresh_order({"status": "drafting", "draft_order": {"u-me": 3}, "league_id": None})
    assert a._slot_locked
    a._refresh_order({"status": "drafting", "draft_order": {"u-me": 11}, "league_id": None})
    assert a._my_slot == 3


def test_explicit_slot_is_never_overridden():
    from ffm.platforms.sleeper import SleeperAdapter

    a = SleeperAdapter("d1", username="me", my_slot=4)
    a._user_id = "u-me"
    a._refresh_order({"status": "drafting", "draft_order": {"u-me": 9}, "league_id": None})
    assert a.my_slot() == 4


def test_board_order_is_priority_not_availability():
    """A safe, zero-urgency player must not outrank an urgent one.

    Ranking by `availability * score` put a QB with VONA +1.6 second on the
    board purely because he was 95% likely to still be there — but high
    availability is a reason to wait on a player, not to prioritize him.
    """
    from ffm.engine.recommend import Recommendation, _first_realistic

    def rec(name, score, hold):
        return Recommendation(
            player=player(name, "RB", 200), score=score, now_gain=score,
            vona=score, survival=0.5, hold=hold, tier_left=3,
        )

    urgent = rec("urgent", 90.0, 0.10)      # elite, probably gone
    mid = rec("mid", 50.0, 0.44)            # good, coin flip
    safe = rec("safe", 16.0, 0.95)          # no urgency at all
    board = sorted([safe, urgent, mid], key=lambda r: -r.score)
    assert [r.player.name for r in board] == ["urgent", "mid", "safe"]

    # The headline still names someone actually gettable.
    assert _first_realistic(board).player.name == "mid"


def test_realistic_falls_back_when_nobody_is_likely():
    from ffm.engine.recommend import Recommendation, _first_realistic

    recs = [
        Recommendation(player=player(f"p{i}", "RB", 200), score=100.0 - i,
                       now_gain=0, vona=0, survival=0.1, hold=0.05 + i * 0.01,
                       tier_left=1)
        for i in range(4)
    ]
    assert _first_realistic(recs).player.name == "p3"   # the most gettable


def test_lookahead_calibration_is_weighted_by_reach():
    """Players already gone must not absorb the next window's pick budget.

    Looking ahead from a future pick, the candidate list still holds players
    who will be long gone by then. Counting them as about-to-be-drafted
    inflated the expected takes far past the number of picks available, and the
    correction then crushed genuinely uncertain players to ~100% survival.
    """
    # Elite players who will be gone well before pick 9.
    gone = [player(f"g{i}", "RB", 300 - i, adp=float(i + 1)) for i in range(8)]
    # A mid-round player who is a real coin flip over picks 9 -> 16.
    mid = [player("mid", "QB", 250, adp=20.0)]
    filler = [player(f"f{i}", "WR", 120, adp=float(25 + i)) for i in range(120)]
    cands = gone + mid + filler

    reach = survival.availability(cands, 1, 9)
    assert reach["g0"] < 0.15                       # gone before pick 9
    assert reach["mid"] > 0.85                      # ADP 20, comfortably there

    naive = survival.availability(cands, 9, 16)["mid"]
    weighted = survival.availability(cands, 9, 16, weights=reach)["mid"]

    # Unweighted, the departed elites eat the pick budget and "mid" reads as
    # certain to survive. Weighted, he keeps a real chance of being taken.
    assert naive > weighted
    assert 0.4 < weighted < 0.95


def test_weighting_leaves_a_same_frame_window_alone():
    """On my own turn nothing has left the board, so weights change nothing."""
    cands = [player(f"p{i}", "RB", 200 - i, adp=float(i + 1)) for i in range(60)]
    ones = {c.player_id: 1.0 for c in cands}
    a = survival.availability(cands, 1, 9)
    b = survival.availability(cands, 1, 9, weights=ones)
    assert a == b


def test_yahoo_preflight_explains_a_missing_fantasy_permission(monkeypatch):
    """An app without Fantasy Sports authenticates fine for other scopes, so
    the failure only appears as invalid_scope after the user has left for the
    browser. Preflight must turn that into a fixable instruction."""
    import ffm.platforms.yahoo as y

    class FakeResp:
        status_code = 302
        headers = {"location": "https://api.login.yahoo.com/oauth2/error"
                               "?client_id=x&error=invalid_scope"}

    monkeypatch.setattr(y.requests, "get", lambda *a, **k: FakeResp())
    msg = y.YahooAuth("id", "secret").preflight()
    assert msg and "Fantasy Sports" in msg and "API Permissions" in msg


def test_yahoo_preflight_passes_a_healthy_app(monkeypatch):
    import ffm.platforms.yahoo as y

    class FakeResp:
        status_code = 302
        headers = {"location": "https://api.login.yahoo.com/oauth2/request_auth/consent"}

    monkeypatch.setattr(y.requests, "get", lambda *a, **k: FakeResp())
    assert y.YahooAuth("id", "secret").preflight() is None


def test_expired_yahoo_code_explains_itself(monkeypatch):
    """Yahoo codes are single-use and last ~60s; the raw 400 body is opaque."""
    import ffm.platforms.yahoo as y
    from ffm.platforms.base import AdapterError

    class FakeResp:
        status_code = 400
        text = '{"error":"invalid_grant","error_description":"Authorization code expired"}'

    monkeypatch.setattr(y.requests, "post", lambda *a, **k: FakeResp())
    with pytest.raises(AdapterError) as e:
        y.YahooAuth("id", "secret").exchange("deadcode")
    assert "expired or already used" in str(e.value)
    assert "--print-url" in str(e.value)


def test_yahoo_403_points_at_app_permissions(monkeypatch):
    """A valid token with no fantasy grant 403s on every endpoint; the raw
    Yahoo message ('not authorized to perform this action') gives the user
    nothing to act on."""
    import ffm.platforms.yahoo as y
    from ffm.platforms.base import AdapterError

    class FakeResp:
        status_code = 403
        text = '{"error":{"description":"This application is not authorized"}}'

    monkeypatch.setattr(y.requests, "get", lambda *a, **k: FakeResp())
    auth = y.YahooAuth("id", "secret")
    auth.token = {"access_token": "t", "refresh_token": "r", "expires_at": 9e9}
    with pytest.raises(AdapterError) as e:
        y.YahooAdapter("461.l.1", auth)._get("league/461.l.1")
    assert "API Permissions" in str(e.value) and "Read" in str(e.value)


@pytest.mark.parametrize("pasted,expected", [
    ("6uc6jqx", "6uc6jqx"),
    ("https://localhost:8000/callback?code=abc123", "abc123"),
    ("https://localhost:8000/callback?code=abc123&state=x", "abc123"),
    ('  "https://localhost:8000/cb?code=xyz"  ', "xyz"),
])
def test_yahoo_accepts_a_bare_code_or_a_pasted_redirect_url(pasted, expected):
    """With a real redirect URI the browser lands on a dead page and the code
    only exists in the address bar, so the whole URL is what gets pasted."""
    from ffm.platforms.yahoo import YahooAuth
    assert YahooAuth.extract_code(pasted) == expected


def test_bad_redirect_uri_blames_the_redirect_not_the_credentials(monkeypatch):
    """invalid_request means the redirect URI did not match the app; pointing
    the user at their client id/secret sends them to the wrong place."""
    import ffm.platforms.yahoo as y

    class FakeResp:
        status_code = 302
        headers = {"location": "https://api.login.yahoo.com/oauth2/error"
                               "?error=invalid_request&error_description=invalid+redirect+uri"}

    monkeypatch.setattr(y.requests, "get", lambda *a, **k: FakeResp())
    msg = y.YahooAuth("id", "secret", redirect_uri="https://localhost:8000/cb").preflight()
    assert "redirect_uri" in msg and "localhost:8000" in msg
    assert "YAHOO_CLIENT_SECRET" not in msg


def test_token_exchange_uses_basic_auth_then_falls_back(monkeypatch):
    """Yahoo documents HTTP Basic for client credentials; the form-body variant
    is accepted too, so it stays as a fallback rather than the default."""
    import base64
    import ffm.platforms.yahoo as y

    calls = []

    class Resp:
        def __init__(self, code):
            self.status_code = code
            self.text = "{}"

        def json(self):
            return {"access_token": "a", "refresh_token": "r", "expires_in": 3600}

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append((data, headers))
        return Resp(200)

    monkeypatch.setattr(y.requests, "post", fake_post)
    monkeypatch.setattr(y.YahooAuth, "save", lambda self: None)
    y.YahooAuth("cid", "sec").exchange("thecode")

    data, headers = calls[0]
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"cid:sec").decode()
    assert "client_secret" not in data          # credentials are in the header
    assert data["code"] == "thecode"


def test_panel_titles_never_render_a_none_pick_number():
    """Once the draft ends there is no 'next pick', and the headline used to
    read 'PLAN FOR PICK #None'."""
    from ffm.engine.recommend import Advice, Recommendation
    from ffm.engine.lineup import Lineup
    from ffm.models import DraftState
    from ffm.ui import dashboard

    st = DraftState(settings=league(teams=2, rounds=1), picks=[], my_slot=1, status="complete")
    rec = Recommendation(player=player("p", "RB", 100), score=1.0, now_gain=1.0,
                         vona=1.0, survival=1.0, hold=1.0, tier_left=1)
    advice = Advice(state=st, available=[], my_players=[], recommendations=[rec],
                    outlook=[], likely_gone=[], is_my_turn=False, realistic=rec,
                    lineup=Lineup(0.0, []), needs=[], this_pick=None, next_pick=None,
                    replacement={}, projected_starters=0.0)

    for panel in (dashboard.recommendation(advice), dashboard.gone_soon(advice)):
        assert "None" not in str(panel.title)


def test_nobody_is_on_the_clock_before_the_draft_starts():
    """Sleeper reports pre_draft until the room opens. Without checking that,
    the slot-1 manager is 'on the clock' — yellow panel and terminal bell —
    from the moment the tool is opened."""
    for slot in (1, 9):
        st = DraftState(settings=league(), picks=[], my_slot=slot, status="pre_draft")
        assert st.has_started is False
        assert st.on_the_clock_slot() is None
        assert st.is_my_turn() is False


def test_the_draft_going_live_is_picked_up():
    """The transition pre_draft -> drafting is what the poll loop must catch."""
    st = DraftState(settings=league(), picks=[], my_slot=1, status="pre_draft")
    assert not st.has_started and not st.is_my_turn()

    st.status = "drafting"                      # next poll sees the new status
    assert st.has_started and st.is_my_turn()   # slot 1 is now genuinely up
    assert st.on_the_clock_slot() == 1


def test_a_first_pick_counts_as_started_even_if_status_lags():
    st = DraftState(settings=league(),
                    picks=[DraftPick(pick_no=1, round=1, slot=1, player_id="x")],
                    my_slot=2, status="pre_draft")
    assert st.has_started
    assert st.on_the_clock_slot() == 2


# ---- roster construction ----------------------------------------------

def test_only_one_kicker_and_defense_are_ever_rostered():
    """The bench branch used to skip K/DEF entirely, so the cap never applied
    and a second defense could take a roster spot owed to a running back."""
    from ffm.engine.recommend import position_cap
    assert position_cap("DEF", league()) == 1
    assert position_cap("K", league()) == 1


def test_running_back_depth_outweighs_receiver_depth():
    """Backs miss time and lose workloads far more than receivers, and the
    flex must not credit whoever currently fills it with an extra starter."""
    from ffm.engine.recommend import _DEPTH_NEED, _bench_value

    settings = league()
    repl = {"RB": 100.0, "WR": 100.0}
    rb = player("rb", "RB", 160.0)
    wr = player("wr", "WR", 160.0)
    # A receiver already holds the FLEX, so WR "starts" 3 and RB 2.
    starters = {"RB": 2, "WR": 3}
    roster = [player(f"r{i}", "RB", 200) for i in range(2)] + \
             [player(f"w{i}", "WR", 200) for i in range(3)]

    rb_val, _ = _bench_value(rb, roster, starters, repl, settings)
    wr_val, _ = _bench_value(wr, roster, starters, repl, settings)
    assert rb_val > wr_val
    assert _DEPTH_NEED["RB"] > _DEPTH_NEED["WR"] > _DEPTH_NEED["TE"] > _DEPTH_NEED["QB"]


def test_serious_injuries_are_penalized_far_harder_than_questionable():
    """A fifth of all draftable players carry a preseason Questionable tag, so
    it is mostly noise; Out/IR/PUP are not."""
    from ffm.engine.recommend import _INJURY_PENALTY
    assert _INJURY_PENALTY["Questionable"] < 0.2
    assert _INJURY_PENALTY["Out"] > 0.5
    assert _INJURY_PENALTY["IR"] > _INJURY_PENALTY["PUP"] > _INJURY_PENALTY["Out"]
