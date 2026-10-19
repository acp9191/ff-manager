"""Tests for the in-season half: context signals, start/sit and waivers.

All offline. The context engine's `build()` is the only part that touches the
network, so these construct an engine and populate its tables directly — the
signals, not the fetching, are what can be wrong in an interesting way.
"""

from __future__ import annotations

import pytest

from ffm.data import scoring as sc
from ffm.data import weekly
from ffm.data.context import (
    _TOTAL_CAP,
    ContextEngine,
    Nudge,
    Opinions,
    TeamForm,
    _drop_injury_exits,
    _role_trend,
    role_view,
)
from ffm import notify
from ffm.engine import startsit, waivers
from ffm.models import LeagueSettings, LeagueWeek, Player, Roster

SLOTS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF"] + ["BN"] * 6


def league(**kw) -> LeagueSettings:
    base = dict(
        platform="test", league_id="1", draft_id="", name="test",
        teams=12, rounds=15, roster_slots=list(SLOTS),
        scoring=sc.default_scoring("half_ppr"), scoring_type="half_ppr",
    )
    base.update(kw)
    return LeagueSettings(**base)


def player(pid, pos, pts, **kw) -> Player:
    return Player(player_id=pid, name=kw.pop("name", pid), position=pos,
                  proj_points=pts, **kw)


def engine(**tables) -> ContextEngine:
    ce = ContextEngine(league(), season="2025", week=10, opinions=Opinions())
    ce.team_form = tables.get("team_form", {})
    ce.def_allowed = tables.get("def_allowed", {})
    ce.pos_baseline = tables.get("pos_baseline", {})
    ce.role = tables.get("role", {})
    return ce


def trend(snap_from, snap_to, opp_from=0.0, opp_to=0.0, games=4.0):
    return {
        "games": games,
        "snap_earlier": snap_from, "snap_recent": snap_to,
        "snap_shift": snap_to - snap_from,
        "opp_earlier": opp_from, "opp_recent": opp_to,
        "opp_shift": opp_to - opp_from,
    }


# ---- context: bounds ---------------------------------------------------

def test_no_signal_can_swing_more_than_the_total_cap():
    """Every signal pointing the same way must still respect the ceiling."""
    ce = engine(
        team_form={"NYJ": TeamForm(ratio=0.3, games=40)},
        def_allowed={("NE", "WR"): (60.0, 40)},
        pos_baseline={"WR": 20.0},
        role={"p": trend(0.9, 0.1)},
    )
    ce.opinions = Opinions(teams={"NYJ": Nudge(-0.35, "no")})
    p = player("p", "WR", 10.0, team="NYJ", opponent="NE")
    adj = ce.adjust(p)
    assert adj.multiplier == pytest.approx(1.0 - _TOTAL_CAP)
    assert adj.points == pytest.approx(7.0)


def test_tiny_projections_are_left_alone():
    """Percentage swings on a 1-point projection are meaningless."""
    ce = engine(team_form={"NYJ": TeamForm(ratio=0.5, games=40)})
    adj = ce.adjust(player("p", "WR", 1.0, team="NYJ"))
    assert adj.multiplier == 1.0
    assert adj.signals == []


def test_a_weak_sample_is_shrunk_toward_no_opinion():
    strong = engine(team_form={"X": TeamForm(ratio=0.92, games=40)})
    weak = engine(team_form={"X": TeamForm(ratio=0.92, games=1)})
    p = lambda: player("p", "WR", 10.0, team="X")
    lean = lambda ce: sum(s.delta for s in ce.adjust(p()).signals)
    assert abs(lean(weak)) < abs(lean(strong))


# ---- context: injury outranks the tilt budget -------------------------

def test_an_out_player_is_zeroed_however_good_the_matchup():
    ce = engine(
        def_allowed={("NE", "WR"): (60.0, 40)},
        pos_baseline={"WR": 20.0},
        team_form={"X": TeamForm(ratio=1.5, games=40)},
    )
    p = player("p", "WR", 15.0, team="X", opponent="NE", injury_status="Out")
    assert ce.adjust(p).points == 0.0


def test_doubtful_survives_but_is_heavily_discounted():
    ce = engine()
    p = player("p", "WR", 10.0, injury_status="Doubtful")
    assert ce.adjust(p).points == pytest.approx(4.0)


# ---- context: role ----------------------------------------------------

def test_quarterbacks_are_judged_on_snaps_not_carries():
    """A QB's share of team rushing is noise; it once read as a benching."""
    t = trend(0.93, 1.0, opp_from=0.19, opp_to=0.05)
    assert role_view(t, "QB")[3] == "snaps"
    assert role_view(t, "QB")[0] > 0
    # A back losing carries is a real signal and must still be caught.
    assert role_view(t, "RB")[3] == "touches"
    assert role_view(t, "RB")[0] < 0


def test_ordinary_snap_wobble_is_not_a_role_change():
    ce = engine(role={"p": trend(0.89, 0.94)})
    assert ce.adjust(player("p", "WR", 12.0)).signals == []


def test_a_real_promotion_registers():
    ce = engine(role={"p": trend(0.35, 0.80)})
    adj = ce.adjust(player("p", "RB", 12.0))
    assert adj.signals[0].delta > 0
    assert "rising role" in adj.summary()


# ---- context: your own read -------------------------------------------

def test_opinions_parse_both_shorthand_and_table_forms(tmp_path):
    f = tmp_path / "opinions.toml"
    f.write_text(
        '[teams]\nNYJ = -0.15\n'
        'NE = { adjust = 0.05, why = "sneaky good" }\n'
        '[players]\n"A.J. Brown" = -0.2\n'
    )
    o = Opinions.load(f)
    assert o.teams["NYJ"].adjust == pytest.approx(-0.15)
    assert o.teams["NE"] == Nudge(0.05, "sneaky good")
    assert o.players["ajbrown"].adjust == pytest.approx(-0.2)


def test_opinions_are_clamped(tmp_path):
    f = tmp_path / "opinions.toml"
    f.write_text("[teams]\nNYJ = -5.0\n")
    assert Opinions.load(f).teams["NYJ"].adjust == pytest.approx(-0.35)


def test_your_read_reaches_the_player_and_says_so(tmp_path):
    f = tmp_path / "opinions.toml"
    f.write_text('[teams]\nNYJ = { adjust = -0.15, why = "not a good team" }\n')
    ce = engine()
    ce.opinions = Opinions.load(f)
    adj = ce.adjust(player("p", "WR", 10.0, team="NYJ"))
    assert adj.points == pytest.approx(8.5)
    assert "not a good team" in adj.summary()


def test_missing_opinions_file_is_not_an_error(tmp_path):
    assert Opinions.load(tmp_path / "nope.toml").teams == {}


def test_an_opinions_path_that_is_a_directory_is_ignored(tmp_path):
    """Docker turns a bind mount of a missing file into a directory."""
    d = tmp_path / "opinions.toml"
    d.mkdir()
    assert Opinions.load(d).teams == {}


def test_unparseable_opinions_do_not_take_the_report_down(tmp_path):
    f = tmp_path / "opinions.toml"
    f.write_text("[teams\nthis is not toml")
    assert Opinions.load(f).teams == {}


# ---- context: the horizon view ----------------------------------------

def test_context_is_shown_but_not_priced():
    """Backtested, team/matchup/role made the projection worse: it already
    reads them. They explain a call; they no longer move the number."""
    ce = engine(
        team_form={"X": TeamForm(ratio=0.80, games=40)},
        def_allowed={("NE", "WR"): (40.0, 40)}, pos_baseline={"WR": 20.0},
        role={"p": trend(0.35, 0.80)},
    )
    adj = ce.adjust(player("p", "WR", 10.0, team="X", opponent="NE"))
    assert {s.kind for s in adj.signals} == {"team", "matchup", "role"}
    assert adj.multiplier == 1.0 and adj.points == 10.0
    assert "soft matchup" in " ".join(adj.notes)


# ---- history window ---------------------------------------------------

def test_history_window_crosses_the_season_boundary_at_half_weight():
    """In September the current season has no games; the layer must still work."""
    w = weekly.history_window("2026", week=1, want=4)
    assert len(w) == 4
    assert {s for s, _, _ in w} == {"2025"}
    assert all(weight == 0.5 for _, _, weight in w)


def test_history_window_never_uses_week_18():
    w = weekly.history_window("2026", week=1, want=6)
    assert all(wk != 18 for _, wk, _ in w)


def test_history_window_prefers_the_current_season():
    w = weekly.history_window("2025", week=10, want=3)
    assert w == [("2025", 9, 1.0), ("2025", 8, 1.0), ("2025", 7, 1.0)]


# ---- weekly scoring regression ----------------------------------------

def test_a_weekly_sized_scoring_gap_falls_back_instead_of_scoring_zero():
    """Team defenses dot-producted to 0.0 under the season-sized guard."""
    stats = {"sack": 3.0, "int": 1.0, "pts_half_ppr": 8.0}
    assert sc.score_player(stats, {"rec": 0.5}, "half_ppr") == 0.0  # season floor
    assert sc.score_player(stats, {"rec": 0.5}, "half_ppr", 3.0) == 8.0


def test_default_scoring_can_actually_score_a_defense():
    stats = {"sack": 3.0, "int": 1.0, "fum_rec": 1.0, "pts_allow_14_20": 1.0}
    assert sc.compute_points(stats, sc.default_scoring("half_ppr")) > 5.0


# ---- start / sit ------------------------------------------------------

def week_state(starters, bench, settings=None):
    settings = settings or league()
    pool = {p.player_id: p for p in starters + bench}
    me = Roster(
        roster_id=1, owner_id="u1", owner_name="You",
        player_ids=[p.player_id for p in starters + bench],
        starters=[p.player_id for p in starters],
    )
    state = LeagueWeek(settings=settings, season="2025", week=10,
                       rosters=[me], my_roster_id=1)
    return state, pool


def basic_roster():
    return [
        player("qb", "QB", 18.0), player("rb1", "RB", 14.0), player("rb2", "RB", 12.0),
        player("wr1", "WR", 13.0), player("wr2", "WR", 11.0), player("te", "TE", 8.0),
        player("flex", "WR", 10.0), player("k", "K", 8.0), player("def", "DEF", 7.0),
    ]


def test_an_optimal_lineup_produces_no_swaps():
    starters = basic_roster()
    bench = [player("b1", "WR", 3.0), player("b2", "RB", 2.0)]
    state, pool = week_state(starters, bench)
    plan = startsit.plan_week(state, pool, engine())
    assert plan.swaps == []
    assert plan.gain == pytest.approx(0.0)


def test_a_better_bench_player_is_surfaced_as_a_swap():
    starters = basic_roster()
    bench = [player("stud", "WR", 25.0)]
    state, pool = week_state(starters, bench)
    plan = startsit.plan_week(state, pool, engine())
    assert [s.start.player_id for s in plan.swaps] == ["stud"]
    # wr2 slides down into FLEX; the player actually displaced is the old flex.
    assert plan.swaps[0].sit.player_id == "flex"
    assert plan.gain > 0


def test_swapping_two_players_between_flex_and_a_named_slot_is_not_a_change():
    """Relabelling RB2 as FLEX is not a decision and must not be reported."""
    starters = basic_roster()
    starters[2], starters[6] = starters[6], starters[2]  # rb2 <-> flex slots
    state, pool = week_state(starters, [])
    plan = startsit.plan_week(state, pool, engine())
    assert plan.swaps == []


def test_injuries_and_your_opinions_still_move_the_number():
    ce = engine(def_allowed={("NE", "WR"): (40.0, 40)}, pos_baseline={"WR": 20.0})
    ce.opinions = Opinions(teams={"X": Nudge(-0.15, "not a good football team")})
    p = player("p", "WR", 10.0, team="X", opponent="NE")
    p.injury_status = "Questionable"
    adj = ce.adjust(p)
    assert adj.points == pytest.approx(10.0 * (1 - 0.15 - 0.10))


def test_context_alone_no_longer_flips_a_start():
    """Two near-identical players; only the matchup separates them."""
    starters = basic_roster()
    starters[4] = player("wr2", "WR", 11.0, opponent="TOUGH")
    bench = [player("alt", "WR", 10.5, opponent="SOFT")]
    ce = engine(
        def_allowed={("TOUGH", "WR"): (10.0, 40), ("SOFT", "WR"): (34.0, 40)},
        pos_baseline={"WR": 22.0},
    )
    state, pool = week_state(starters, bench)
    plan = startsit.plan_week(state, pool, ce)
    assert plan.swaps == []


def test_noise_level_gains_are_not_reported_as_swaps():
    starters = basic_roster()
    bench = [player("marginal", "WR", 10.0 + startsit.MIN_SWAP_GAIN / 2)]
    state, pool = week_state(starters, bench)
    assert startsit.plan_week(state, pool, engine()).swaps == []


def test_a_bye_week_starter_is_flagged():
    starters = basic_roster()
    starters[3] = player("wr1", "WR", 0.0, bye_week=10)
    state, pool = week_state(starters, [])
    plan = startsit.plan_week(state, pool, engine())
    assert any("BYE" in a.text and a.level == "urgent" for a in plan.alerts)


def test_an_injured_starter_is_flagged():
    starters = basic_roster()
    starters[1] = player("rb1", "RB", 14.0, injury_status="Out")
    state, pool = week_state(starters, [])
    plan = startsit.plan_week(state, pool, engine())
    assert any("Out" in a.text and a.level == "urgent" for a in plan.alerts)


def test_an_empty_lineup_slot_is_flagged():
    starters = basic_roster()[:-1]  # no DEF
    state, pool = week_state(starters, [])
    plan = startsit.plan_week(state, pool, engine())
    assert any("empty" in a.text for a in plan.alerts)


def test_the_recommended_lineup_never_leaves_a_fillable_slot_empty():
    starters = basic_roster()
    state, pool = week_state(starters, [])
    plan = startsit.plan_week(state, pool, engine())
    assert all(call is not None for _, call in plan.by_slot())


# ---- waivers ----------------------------------------------------------

def bench_roster():
    """A starting nine plus two spare parts, so something is droppable."""
    return basic_roster() + [player("bench1", "WR", 4.0), player("bench2", "RB", 3.0)]


def full_roster():
    """Filled to the 15 slots, so a pickup genuinely costs someone his place."""
    filler = [player(f"filler{i}", "WR", 5.0 + i) for i in range(4)]
    return bench_roster() + filler


def waiver_setup(free_agents, mine=None, market=None, horizon=None):
    mine = mine or bench_roster()
    pool = {p.player_id: p for p in mine + free_agents}
    me = Roster(roster_id=1, owner_id="u1", owner_name="You",
                player_ids=[p.player_id for p in mine],
                starters=[p.player_id for p in mine[:9]])
    state = LeagueWeek(settings=league(), season="2025", week=10,
                       rosters=[me], my_roster_id=1)
    horizon = horizon or {p.player_id: p.proj_points for p in pool.values()}
    return state, pool, horizon, (market or {})


def test_rostered_players_are_never_offered_as_pickups():
    state, pool, horizon, market = waiver_setup([player("fa", "WR", 20.0)])
    board = waivers.build_board(state, pool, engine(), horizon, market)
    owned = state.rostered_ids()
    assert all(p.player.player_id not in owned for p in board.pickups)
    assert "fa" in {p.player.player_id for p in board.pickups}


def test_a_player_on_ir_is_not_a_pickup_however_hot_he_looks():
    fa = player("hurt", "RB", 0.0, injury_status="PUP")
    ce = engine(role={"hurt": trend(0.2, 0.9)})
    state, pool, horizon, _ = waiver_setup([fa])
    board = waivers.build_board(state, pool, ce, horizon, {"hurt": 200000})
    assert "hurt" not in {p.player.player_id for p in board.pickups}


def test_a_role_change_surfaces_a_player_the_projections_have_not_caught():
    """The point of watching the wire: usage moves before points do."""
    fa = player("riser", "RB", 4.0)
    ce = engine(role={"riser": trend(0.2, 0.75, games=4.0)})
    state, pool, horizon, _ = waiver_setup([fa])
    board = waivers.build_board(state, pool, ce, horizon, {})
    top = board.pickups[0]
    assert top.player.player_id == "riser"
    assert top.kind == "breakout"
    assert "role jumped" in top.why[0]


def test_the_add_market_is_scored_but_cannot_dominate():
    quiet = player("quiet", "WR", 15.0)
    hyped = player("hyped", "WR", 2.0)
    state, pool, horizon, _ = waiver_setup([quiet, hyped])
    board = waivers.build_board(state, pool, engine(), horizon, {"hyped": 250000})
    order = [p.player.player_id for p in board.pickups]
    assert order.index("quiet") < order.index("hyped")


def test_the_drop_suggestion_is_never_a_player_you_are_told_to_start():
    state, pool, horizon, market = waiver_setup([player("fa", "WR", 20.0)],
                                                mine=full_roster())
    board = waivers.build_board(state, pool, engine(), horizon, market)
    plan = startsit.plan_week(state, pool, engine())
    starting = {c.player.player_id for c in plan.calls if c.slot}
    assert board.pickups and board.drops
    for p in board.pickups:
        assert p.drop is not None and p.drop.player_id not in starting
    assert all(d.player.player_id not in starting for d in board.drops)


def test_the_drop_depends_on_what_you_are_adding():
    """Like-for-like never thins a position, so an RB add can cut an RB."""
    state, pool, horizon, market = waiver_setup(
        [player("fa1", "WR", 20.0), player("fa2", "RB", 19.0)], mine=full_roster()
    )
    board = waivers.build_board(state, pool, engine(), horizon, market)
    drops = {p.player.position: p.drop for p in board.pickups}
    # The roster carries exactly the floor at RB, so only an RB add may cut one.
    assert drops["RB"].position == "RB"
    assert drops["WR"].position != "RB"


def test_pickups_are_valued_over_the_horizon_not_this_sunday():
    """A player on bye this week can still be the right add."""
    fa = player("bye_guy", "RB", 0.0, bye_week=10)
    state, pool, horizon, _ = waiver_setup([fa])
    horizon["bye_guy"] = 18.0
    board = waivers.build_board(state, pool, engine(), horizon, {})
    assert board.pickups[0].player.player_id == "bye_guy"
    assert board.pickups[0].horizon_gain > 0


# ---- models -----------------------------------------------------------

def test_rostered_ids_spans_every_team_and_the_ir():
    a = Roster(roster_id=1, owner_id="a", owner_name="A",
               player_ids=["1", "2"], reserve=["9"])
    b = Roster(roster_id=2, owner_id="b", owner_name="B", player_ids=["3"])
    lw = LeagueWeek(settings=league(), season="2025", week=1,
                    rosters=[a, b], my_roster_id=1)
    assert lw.rostered_ids() == {"1", "2", "3", "9"}
    assert lw.me.roster_id == 1


def test_bench_is_whatever_is_not_starting():
    r = Roster(roster_id=1, owner_id="a", owner_name="A",
               player_ids=["1", "2", "3"], starters=["1"])
    assert r.bench_ids == ["2", "3"]


# ---- Sleeper league parsing -------------------------------------------
# The one path that cannot be exercised without a real league id, so it is
# pinned against Sleeper's documented response shapes instead.

LEAGUE_JSON = {
    "league_id": "987", "name": "Dynasty Warriors", "season": "2025",
    "total_rosters": 12, "status": "in_season", "draft_id": "555",
    "roster_positions": ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF",
                         "BN", "BN", "BN", "BN", "BN", "IR"],
    "scoring_settings": {"rec": 1.0, "rec_yd": 0.1, "pass_td": 4.0, "bogus": "x"},
}

ROSTERS_JSON = [
    {"roster_id": 1, "owner_id": "u1",
     "players": ["10", "11", "12"], "starters": ["10", "0", "12"],
     "reserve": ["99"], "settings": {"wins": 6, "losses": 4, "ties": 0}},
    {"roster_id": 2, "owner_id": "u2", "players": ["20"], "starters": ["20"],
     "settings": {"wins": 3, "losses": 7}},
]

USERS_JSON = [
    {"user_id": "u1", "display_name": "avery", "metadata": {"team_name": "Team Chaos"}},
    {"user_id": "u2", "display_name": "sam", "metadata": {}},
]


@pytest.fixture
def fake_league(monkeypatch):
    from ffm.platforms import league as lg

    def fake_get(path):
        if path == "/league/987":
            return LEAGUE_JSON
        if path == "/league/987/rosters":
            return ROSTERS_JSON
        if path == "/league/987/users":
            return USERS_JSON
        if path == "/league/987/matchups/10":
            return [{"roster_id": 1, "matchup_id": 3},
                    {"roster_id": 2, "matchup_id": 3}]
        return None

    monkeypatch.setattr(lg, "_get", fake_get)
    monkeypatch.setattr(lg, "user_id", lambda name: {"avery": "u1"}.get(name, "zz"))
    return lg


def test_league_settings_are_normalized(fake_league):
    s = fake_league.SleeperLeague("987").settings()
    assert s.name == "Dynasty Warriors"
    assert s.teams == 12
    assert s.starter_slots == ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF"]
    assert s.scoring_type == "ppr"
    assert "bogus" not in s.scoring  # non-numeric settings are dropped


def test_unfilled_lineup_slots_are_not_treated_as_players(fake_league):
    """Sleeper pads `starters` with "0" for an empty slot."""
    r = fake_league.SleeperLeague("987").rosters()[0]
    assert r.starters == ["10", "12"]
    assert r.record == "6-4"


def test_a_team_name_beats_a_display_name(fake_league):
    rosters = fake_league.SleeperLeague("987").rosters()
    assert rosters[0].owner_name == "Team Chaos"
    assert rosters[1].owner_name == "sam"


def test_your_roster_is_found_from_your_username(fake_league):
    lg = fake_league.SleeperLeague("987", username="avery")
    assert lg.my_roster_id(lg.rosters()) == 1


def test_an_unknown_username_says_so(fake_league):
    from ffm.platforms.base import AdapterError
    lg = fake_league.SleeperLeague("987", username="nobody")
    with pytest.raises(AdapterError, match="does not own a team"):
        lg.my_roster_id(lg.rosters())


def test_the_weekly_opponent_comes_from_the_shared_matchup_id(fake_league):
    lg = fake_league.SleeperLeague("987", username="avery")
    assert lg.opponent_roster_id(10, 1) == 2


def test_ir_players_still_count_as_rostered(fake_league):
    lg = fake_league.SleeperLeague("987", username="avery")
    assert "99" in lg.week_state(10).rostered_ids()


def test_a_missing_league_explains_how_to_find_the_right_id(fake_league):
    from ffm.platforms.base import AdapterError
    with pytest.raises(AdapterError, match="ffm leagues"):
        fake_league.SleeperLeague("nope").settings()


# ---- how far a team-level opinion reaches -----------------------------
# Fading an offense should move the players who share in it. A kicker's value
# partly inverts with offensive quality, and a team defense scores off
# entirely separate events, so neither is included by default.

def team_faded(position, **kw):
    ce = engine()
    ce.opinions = Opinions(teams={"NYJ": Nudge(-0.15, "bad team", **kw)})
    return ce.adjust(player("p", position, 10.0, team="NYJ"))


@pytest.mark.parametrize("position", ["QB", "RB", "WR", "TE"])
def test_a_team_fade_moves_the_offence(position):
    assert team_faded(position).points == pytest.approx(8.5)


@pytest.mark.parametrize("position", ["K", "DEF"])
def test_a_team_fade_leaves_the_kicker_and_defence_alone(position):
    adj = team_faded(position)
    assert adj.points == pytest.approx(10.0)
    assert adj.signals == []


def test_a_team_entry_can_opt_positions_back_in():
    assert team_faded("K", positions=("K",)).points == pytest.approx(8.5)
    # ...and opting one in does not silently keep the defaults.
    assert team_faded("RB", positions=("K",)).points == pytest.approx(10.0)


def test_naming_a_defence_directly_still_works():
    """The escape hatch: [players] is unambiguous, so position never filters."""
    ce = engine()
    ce.opinions = Opinions(players={"newyorkjets": Nudge(-0.2, "bad unit")})
    adj = ce.adjust(player("p", "DEF", 10.0, team="NYJ", name="New York Jets"))
    assert adj.points == pytest.approx(8.0)
    assert "bad unit" in adj.summary()


def test_positions_parse_from_toml(tmp_path):
    f = tmp_path / "opinions.toml"
    f.write_text('[teams]\nNYJ = { adjust = -0.1, positions = ["rb", "wr"] }\n')
    assert Opinions.load(f).teams["NYJ"].positions == ("RB", "WR")


def test_a_typo_in_positions_is_dropped_not_fatal(tmp_path):
    f = tmp_path / "opinions.toml"
    f.write_text('[teams]\nNYJ = { adjust = -0.1, positions = ["RB", "WIDEOUT"] }\n')
    assert Opinions.load(f).teams["NYJ"].positions == ("RB",)


def test_an_entry_covering_nothing_simply_does_nothing(tmp_path):
    f = tmp_path / "opinions.toml"
    f.write_text('[teams]\nNYJ = { adjust = -0.1, positions = ["NONSENSE"] }\n')
    o = Opinions.load(f)
    ce = engine()
    ce.opinions = o
    assert ce.adjust(player("p", "RB", 10.0, team="NYJ")).signals == []


# ---- should I actually make the move? ---------------------------------
# "Pick up X" is half an answer. These cover the other half.

from ffm.engine.roster_move import CLEAR, MoveAdvisor  # noqa: E402


def advisor_for(mine, free_agent, horizon=None):
    pool = {p.player_id: p for p in mine + [free_agent]}
    me = Roster(roster_id=1, owner_id="u1", owner_name="You",
                player_ids=[p.player_id for p in mine],
                starters=[p.player_id for p in mine[:9]])
    state = LeagueWeek(settings=league(), season="2025", week=10,
                       rosters=[me], my_roster_id=1)
    horizon = horizon or {p.player_id: p.proj_points for p in pool.values()}
    return MoveAdvisor(state, pool, engine(), horizon, horizon_weeks=4)


def test_an_open_roster_spot_means_nobody_has_to_go():
    """The question that was never being asked."""
    adv = advisor_for(bench_roster(), player("fa", "RB", 9.0))   # 11 of 15 used
    move = adv.evaluate(player("fa", "RB", 9.0))
    assert move.open_spots == 4
    assert move.drop is None
    assert move.verdict == "add"
    assert "open roster spot" in " ".join(move.lines)


def test_a_full_roster_forces_a_choice():
    adv = advisor_for(full_roster(), player("fa", "RB", 9.0))
    move = adv.evaluate(player("fa", "RB", 9.0))
    assert move.open_spots == 0
    assert move.drop is not None


def test_players_on_ir_do_not_occupy_an_active_spot():
    mine = full_roster()
    pool = {p.player_id: p for p in mine}
    me = Roster(roster_id=1, owner_id="u1", owner_name="You",
                player_ids=[p.player_id for p in mine],
                starters=[p.player_id for p in mine[:9]],
                reserve=[mine[-1].player_id])
    state = LeagueWeek(settings=league(), season="2025", week=10,
                       rosters=[me], my_roster_id=1)
    horizon = {p.player_id: p.proj_points for p in pool.values()}
    adv = MoveAdvisor(state, pool, engine(), horizon)
    assert adv.open_spots == 1


def test_a_clear_upgrade_says_so_and_shows_the_net():
    adv = advisor_for(full_roster(), player("stud", "RB", 18.0))
    move = adv.evaluate(player("stud", "RB", 18.0))
    assert move.verdict == "add"
    assert move.net >= CLEAR
    assert any("Net +" in l for l in move.lines)


def test_a_move_that_costs_you_points_says_so():
    """The failure mode: an exciting headline hiding a negative trade."""
    adv = advisor_for(full_roster(), player("meh", "WR", 1.0))
    move = adv.evaluate(player("meh", "WR", 1.0))
    assert move.verdict == "skip"
    assert move.net < 0
    assert not move.do_it


def test_a_speculative_add_is_labelled_rather_than_dressed_up():
    adv = advisor_for(full_roster(), player("rookie", "RB", 1.5))
    move = adv.evaluate(player("rookie", "RB", 1.5), speculative=True)
    assert move.verdict == "speculative"
    assert "role, not his projection" in " ".join(move.lines)


def test_a_roster_with_no_spare_parts_reports_no_free_cut():
    """Every player startable: there is no painless move, and it should say so."""
    mine = basic_roster()          # exactly nine, all of them starters
    adv = advisor_for(mine, player("fa", "RB", 9.0))
    adv.open_spots = 0             # pretend the league has no bench
    move = adv.evaluate(player("fa", "RB", 9.0))
    assert move.verdict == "hold"
    assert "no safe cut" in " ".join(move.lines)


def test_the_named_drop_is_the_least_valuable_spare_player():
    adv = advisor_for(full_roster(), player("fa", "RB", 9.0))
    move = adv.evaluate(player("fa", "RB", 9.0))
    assert move.drop.player_id == "bench2"      # 3.0/wk, the lowest
    assert "does not thin a position" in " ".join(move.lines)


def test_a_promotion_shows_the_workload_that_came_free():
    """His own projection predates the promotion, so quoting it alone misleads."""
    injured = player("starter", "RB", 14.0, name="Star Back")
    heir = player("heir", "RB", 1.5, name="Backup Guy")
    adv = advisor_for(full_roster() + [injured], heir,
                      horizon={"starter": 14.0, "heir": 1.5,
                               **{p.player_id: p.proj_points
                                  for p in full_roster()}})
    move = adv.evaluate(heir, speculative=True, vacated=injured)
    text = " ".join(move.lines)
    assert "predates the promotion" in text
    assert "Star Back" in text and "14.0" in text


def test_a_big_vacated_role_reads_as_a_real_pickup_not_a_flyer():
    """Stale arithmetic said 'skip' for exactly the moves worth making, and
    calling it merely speculative would have muted it once flyers were."""
    injured = player("starter", "RB", 14.0, name="Star Back")
    heir = player("heir", "RB", 1.5, name="Backup Guy")
    roster = full_roster()
    horizon = {"starter": 14.0, "heir": 1.5,
               **{p.player_id: p.proj_points for p in roster}}
    adv = advisor_for(roster + [injured], heir, horizon=horizon)
    move = adv.evaluate(heir, speculative=True, vacated=injured)
    assert move.verdict == "inherit"
    assert move.actionable, "a job coming free is worth a notification"
    # Without a vacated role behind it, the same weak player is just a skip.
    assert adv.evaluate(heir).verdict == "skip"


# ---- positional insurance ---------------------------------------------
# Ranking on points alone recommended cutting a manager's only backup RB: he
# was the lowest-scoring non-starter, which is the definition of a backup.
# Points do not measure what he is for.

from ffm.engine.roster_move import depth_floor  # noqa: E402


def thin_at_rb():
    """Two RBs starting, one covering them, and a surplus of receivers."""
    return [
        player("qb", "QB", 18.0), player("rb1", "RB", 15.0), player("rb2", "RB", 12.0),
        # Cheapest spare on the roster, which is exactly why the old logic
        # reached for him first. This mirrors the real roster that reported it.
        player("rb3", "RB", 4.0),                      # the only backup
        player("wr1", "WR", 16.0), player("wr2", "WR", 12.0), player("wr3", "WR", 10.0),
        player("wr4", "WR", 9.0), player("wr5", "WR", 8.0), player("wr6", "WR", 7.9),
        player("te1", "TE", 11.0), player("te2", "TE", 9.3),
        player("k", "K", 6.9), player("def1", "DEF", 7.6), player("def2", "DEF", 7.5),
    ]


def test_the_only_backup_running_back_is_not_the_drop():
    """The bug, pinned."""
    adv = advisor_for(thin_at_rb(), player("fa", "WR", 11.0))
    move = adv.evaluate(player("fa", "WR", 11.0))
    assert move.drop is not None
    assert move.drop.player_id != "rb3"
    assert move.drop.position != "RB"


def test_it_says_why_it_spared_him():
    adv = advisor_for(thin_at_rb(), player("fa", "WR", 11.0))
    move = adv.evaluate(player("fa", "WR", 11.0))
    assert move.kept_for_depth == "rb3"
    assert "cover a bye or an injury" in " ".join(move.lines)


def test_it_still_takes_the_cheapest_safe_cut():
    """Sparing the back must not mean cutting someone expensive instead."""
    adv = advisor_for(thin_at_rb(), player("fa", "WR", 11.0))
    move = adv.evaluate(player("fa", "WR", 11.0))
    assert move.drop.player_id == adv.safe_drops[0].player_id
    assert move.drop_value <= 7.6


def test_adding_a_back_may_cut_the_backup_back():
    """Like for like leaves the count where it started, so it is allowed."""
    adv = advisor_for(thin_at_rb(), player("fa", "RB", 14.0))
    assert adv.evaluate(player("fa", "RB", 14.0)).drop.player_id == "rb3"


def test_but_a_receiver_may_not_cut_that_same_back():
    """The identical player, spared or not depending on what replaces him."""
    adv = advisor_for(thin_at_rb(), player("fa", "WR", 14.0))
    assert adv.evaluate(player("fa", "WR", 14.0)).drop.player_id != "rb3"


def test_streamable_positions_need_no_insurance():
    st = league()
    assert depth_floor(st, "K") == 1        # one starter, no spare
    assert depth_floor(st, "DEF") == 1
    assert depth_floor(st, "QB") == 1       # a second QB never plays in 1QB
    assert depth_floor(st, "RB") == 3       # two starters plus a body
    assert depth_floor(st, "WR") == 3
    assert depth_floor(st, "TE") == 2


def test_superflex_makes_a_second_quarterback_real_depth():
    sf = league(roster_slots=["QB", "RB", "RB", "WR", "WR", "TE", "SUPER_FLEX",
                              "K", "DEF"] + ["BN"] * 6)
    assert depth_floor(sf, "QB") == 2


def test_a_roster_with_no_safe_cut_says_so_instead_of_forcing_one():
    thin = [player("qb", "QB", 18.0), player("rb1", "RB", 15.0),
            player("rb2", "RB", 12.0), player("rb3", "RB", 9.0),
            player("wr1", "WR", 16.0), player("wr2", "WR", 12.0),
            player("wr3", "WR", 10.0), player("te1", "TE", 11.0),
            player("te2", "TE", 9.0), player("k", "K", 7.0),
            player("def1", "DEF", 7.0)]
    adv = advisor_for(thin, player("fa", "WR", 11.0))
    adv.open_spots = 0
    move = adv.evaluate(player("fa", "WR", 11.0))
    assert move.verdict == "hold"
    assert "no safe cut" in " ".join(move.lines)


# ---- week 2 retro regressions ------------------------------------------

def _use(snap, tgt=0.1):
    return {"snap_share": snap, "target_share": tgt, "carry_share": 0.0}


def test_last_seasons_finale_is_never_part_of_the_recent_sample():
    """Week 2: one game this season, then last year's week 17 finale.

    Pairing them read an 82%-snap tight end as "shrinking, 91% -> 53%".
    """
    rows = [(0, _use(0.82), True), (1, _use(0.23), False),
            (2, _use(0.86), False), (3, _use(0.96), False)]
    t = _role_trend(rows)
    assert t["snap_shift"] == 0.0 and t["opp_shift"] == 0.0


def test_two_games_this_season_make_a_recent_sample():
    rows = [(0, _use(0.90), True), (1, _use(0.80), True),
            (2, _use(0.50), False), (3, _use(0.50), False)]
    t = _role_trend(rows)
    assert t["snap_recent"] == pytest.approx(0.85)
    assert t["snap_earlier"] == pytest.approx(0.50)


# ---- week 5 retro regressions ------------------------------------------

_MONTGOMERY = [(0, _use(0.14), True), (1, _use(0.56), True),
               (2, _use(0.63), True), (3, _use(0.53), True),
               (4, _use(0.49), True)]


def test_a_game_he_left_hurt_is_not_a_shrinking_role():
    """Montgomery, week 5: 9 of 66 snaps, then a groin injury. Still tagged."""
    t = _role_trend(_drop_injury_exits(_MONTGOMERY, missed=set(), tagged=True))
    assert t["snap_shift"] > -0.06  # no longer reads as a role change


def test_the_injury_game_stays_out_after_he_misses_the_next_one():
    """He sat week 6 and the tag has cleared: the week 5 exit is still not his role."""
    rows = [(o + 1, u, c) for o, u, c in _MONTGOMERY]
    kept = _drop_injury_exits(rows, missed={0}, tagged=False)
    assert [o for o, _, _ in kept] == [2, 3, 4, 5]


def test_a_healthy_benching_still_counts():
    """Same snap collapse, no tag, no missed game: that is a demotion."""
    kept = _drop_injury_exits(_MONTGOMERY, missed=set(), tagged=False)
    assert kept == _MONTGOMERY
    assert _role_trend(kept)["snap_shift"] < -0.06


def test_an_ordinary_down_week_while_tagged_still_counts():
    """A tag alone is not evidence: the game has to have collapsed too."""
    rows = [(0, _use(0.45), True)] + _MONTGOMERY[1:]
    assert _drop_injury_exits(rows, missed=set(), tagged=True) == rows


def test_a_swap_leads_with_a_reason_for_it_not_against_it():
    """The digest shows one line; it must not be the case against the start."""
    starters = basic_roster()
    starters[5] = player("fannin", "TE", 7.0, opponent="SOFT")
    bench = [player("loveland", "TE", 11.0, opponent="TOUGH")]
    ce = engine(
        def_allowed={("TOUGH", "TE"): (6.0, 40), ("SOFT", "TE"): (21.0, 40)},
        pos_baseline={"TE": 11.0},
    )
    state, pool = week_state(starters, bench)
    plan = startsit.plan_week(state, pool, ce)
    [swap] = plan.swaps
    assert swap.start.player_id == "loveland"
    assert swap.why[0].startswith("projected 11.0 to 7.0")
    against = [w for w in swap.why if "tough matchup" in w]
    assert against and all(w.startswith("despite:") for w in against)


def test_the_matchup_line_quotes_the_lineup_you_would_field():
    """A sub-threshold optimizer change must not leak into the projection."""
    starters = basic_roster()
    bench = [player("marginal", "WR", 10.0 + startsit.MIN_SWAP_GAIN / 2)]
    opp = Roster(roster_id=2, owner_id="u2", owner_name="Them",
                 player_ids=["o1"], starters=["o1"])
    state, pool = week_state(starters, bench)
    pool["o1"] = player("o1", "QB", 20.0)
    plan = startsit.plan_week(state, pool, engine(), opponent=opp)
    assert plan.swaps == [] and plan.recommended_points > plan.current_points
    lines = notify.digest_message(plan).lines
    assert lines[0] == (f"vs Them — projected {plan.current_raw:.1f} to 20.0")
    assert "lineup is already optimal" in "\n".join(lines)


# ---- horizon: byes -----------------------------------------------------

def _horizon_feed(monkeypatch, weeks: dict[int, list[dict]], byes: dict[str, int]):
    monkeypatch.setattr(weekly, "weekly_projections", lambda season, w: weeks.get(w, []))
    monkeypatch.setattr(weekly, "bye_weeks", lambda season=None: byes)
    monkeypatch.setattr(weekly, "all_players", lambda: {
        "k_bye": {"team": "CIN"}, "k_hurt": {"team": "LV"}, "CIN": {"team": "CIN"},
    })


def test_a_bye_in_the_window_is_not_priced_as_a_zero(monkeypatch):
    """McPherson: 6.6 a game, bye in week 6. He is a 6.6 kicker, not a 4.9 one."""
    row = lambda pid: {"player_id": pid, "stats": {"rec_yd": 66.0}}
    _horizon_feed(monkeypatch, {3: [row("k_bye")], 4: [row("k_bye")],
                                5: [row("k_bye")], 6: []}, {"CIN": 6})
    h = weekly.horizon_points(league(), week=3, weeks=4, season="2026")
    assert h["k_bye"] == pytest.approx(6.6)


def test_a_missed_game_that_is_not_a_bye_still_counts_as_zero(monkeypatch):
    """An injury absence is real lost value; only the bye is forgiven."""
    row = lambda pid: {"player_id": pid, "stats": {"rec_yd": 80.0}}
    _horizon_feed(monkeypatch, {3: [row("k_hurt")], 4: [row("k_hurt")],
                                5: [row("k_hurt")], 6: []}, {"LV": 9})
    h = weekly.horizon_points(league(), week=3, weeks=4, season="2026")
    assert h["k_hurt"] == pytest.approx(6.0)


def test_a_defense_finds_its_own_bye(monkeypatch):
    row = {"player_id": "CIN", "stats": {"rec_yd": 70.0}}
    _horizon_feed(monkeypatch, {3: [row], 4: [row], 5: [row], 6: []}, {"CIN": 6})
    h = weekly.horizon_points(league(), week=3, weeks=4, season="2026")
    assert h["CIN"] == pytest.approx(7.0)


# ---- net is what reaches your lineup ------------------------------------

def test_a_backup_quarterback_is_worth_nothing_to_a_one_qb_team():
    """Malik Willis: 17.8 a week behind a 20-point starter read "+11.1, Add him"."""
    backup = player("willis", "QB", 16.0)
    adv = advisor_for(full_roster(), backup)
    move = adv.evaluate(backup, speculative=True)
    assert move.never_plays and move.value_to_you == 0.0
    assert move.net < 0
    assert move.verdict == "skip" and not move.actionable
    assert "never plays" in " ".join(move.lines)


def test_a_quarterback_who_would_start_is_still_an_add():
    upgrade = player("upgrade", "QB", 25.0)
    adv = advisor_for(full_roster(), upgrade)
    move = adv.evaluate(upgrade)
    assert not move.never_plays
    assert move.verdict == "add" and move.actionable


def test_a_spare_kicker_never_plays_either():
    spare = player("spare_k", "K", 7.5)
    move = advisor_for(full_roster(), spare).evaluate(spare)
    assert move.never_plays and move.verdict == "skip"


def test_a_bench_upgrade_shows_on_the_board_but_does_not_buzz_your_phone():
    """Depth at a FLEX position is worth something; it is not worth an alert."""
    depth = player("depth", "WR", 9.5)          # below every starting WR and FLEX
    move = advisor_for(full_roster(), depth).evaluate(depth)
    assert not move.never_plays
    assert move.lineup_gain < 1.0 and move.do_it
    assert not move.actionable


def test_a_kicker_who_would_start_by_a_little_is_not_told_he_never_plays():
    """Under the bar for recommending the move, but he does play."""
    slightly = player("gay", "K", 8.5)          # starter "k" projects 8.0
    move = advisor_for(full_roster(), slightly).evaluate(slightly)
    assert 0 < move.lineup_gain < 1.0
    assert not move.never_plays
    assert "never plays" not in " ".join(move.lines)


# ---- locked players: a game that has kicked off -------------------------

def test_a_starter_hurt_during_his_game_is_not_benched():
    """Coker went Out mid-game; he had already played, so there is nothing to do."""
    starters = basic_roster()
    starters[4] = player("coker", "WR", 11.0, injury_status="Out")
    bench = [player("evans", "WR", 10.0)]
    state, pool = week_state(starters, bench)
    plan = startsit.plan_week(state, pool, engine(), locked={"coker"})
    assert plan.swaps == []
    assert not any("coker" == a.player_id for a in plan.alerts)
    assert plan.locked_starters == 1


def test_the_same_injury_before_kickoff_is_still_fixed():
    starters = basic_roster()
    starters[4] = player("coker", "WR", 11.0, injury_status="Out")
    bench = [player("evans", "WR", 10.0)]
    state, pool = week_state(starters, bench)
    plan = startsit.plan_week(state, pool, engine())
    assert [s.sit.player_id for s in plan.swaps] == ["coker"]
    assert any(a.player_id == "coker" and a.level == "urgent" for a in plan.alerts)


def test_a_bench_player_whose_game_started_cannot_come_in():
    starters = basic_roster()
    bench = [player("stud", "WR", 25.0)]
    state, pool = week_state(starters, bench)
    plan = startsit.plan_week(state, pool, engine(), locked={"stud"})
    assert plan.swaps == []


def test_unlocked_slots_are_still_optimized_around_locked_ones():
    starters = basic_roster()
    bench = [player("stud", "WR", 25.0)]
    state, pool = week_state(starters, bench)
    plan = startsit.plan_week(state, pool, engine(), locked={"wr1", "flex"})
    [swap] = plan.swaps
    assert swap.start.player_id == "stud" and swap.sit.player_id == "wr2"


def test_game_status_is_read_per_team_for_the_week(monkeypatch):
    games = [{"week": 3, "home": "CLE", "away": "CAR", "status": "in_game"},
             {"week": 4, "home": "CAR", "away": "NE", "status": "pre_game"}]
    monkeypatch.setattr(weekly.cache, "cached", lambda key, ttl, fetch: games)
    assert weekly.game_status("2026", 3) == {"CLE": "in_game", "CAR": "in_game"}
    assert weekly.game_status("2026", 4)["CAR"] == "pre_game"


# ---- game-time decisions ----------------------------------------------

def test_a_questionable_player_needs_a_real_edge_over_a_healthy_one():
    """Week 4: +1.1 for Coker (Questionable) over Vele; Coker was ruled out."""
    starters = basic_roster()
    starters[6] = player("vele", "WR", 9.6)                 # FLEX
    bench = [player("coker", "WR", 12.0, injury_status="Questionable")]
    state, pool = week_state(starters, bench)
    plan = startsit.plan_week(state, pool, engine())
    assert plan.swaps == []


def test_a_questionable_player_with_a_big_edge_is_still_recommended():
    starters = basic_roster()
    starters[6] = player("vele", "WR", 6.0)
    bench = [player("coker", "WR", 14.0, injury_status="Questionable")]
    state, pool = week_state(starters, bench)
    [swap] = startsit.plan_week(state, pool, engine()).swaps
    assert swap.start.player_id == "coker"
    assert any("despite: listed Questionable" in w for w in swap.why)


def test_a_questionable_player_may_still_replace_one_who_is_out():
    starters = basic_roster()
    starters[6] = player("hurt", "WR", 10.0, injury_status="Out")
    bench = [player("coker", "WR", 11.0, injury_status="Questionable")]
    state, pool = week_state(starters, bench)
    [swap] = startsit.plan_week(state, pool, engine()).swaps
    assert swap.start.player_id == "coker" and swap.sit.player_id == "hurt"
