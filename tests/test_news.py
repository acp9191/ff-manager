"""Tests for the change detector.

Two failure modes matter and they pull against each other: missing the injury
that opens a job, and producing a wall of notifications nobody reads. Most of
these are about the second one.
"""

from __future__ import annotations

import pytest

from ffm.data import scoring as sc
from ffm.engine import news
from ffm.models import LeagueSettings, LeagueWeek, Player, Roster

SLOTS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF"] + ["BN"] * 6


def league(rosters, week=5):
    st = LeagueSettings(platform="t", league_id="1", draft_id="", name="t",
                        teams=2, rounds=15, roster_slots=list(SLOTS),
                        scoring=sc.default_scoring("half_ppr"))
    return LeagueWeek(settings=st, season="2025", week=week,
                      rosters=rosters, my_roster_id=1)


def player(pid, pos="RB", team="NYJ", pts=10.0, depth=None, status=None, name=None):
    return Player(player_id=pid, name=name or pid, position=pos, team=team,
                  proj_points=pts, depth_chart_order=depth, injury_status=status)


def snap(injuries=None, market=None):
    return news.Snapshot(injuries=dict(injuries or {}), market=dict(market or {}))


# ---- severity ----------------------------------------------------------

def test_worsening_is_directional():
    assert news._worsened(None, "Out")
    assert news._worsened("Questionable", "Out")
    assert not news._worsened("Out", "Questionable")   # recovery is not an alert
    assert not news._worsened("Out", "Out")


# ---- injury noise control ---------------------------------------------

def test_a_cold_start_reports_no_injury_changes():
    """With no previous poll, every existing tag would read as breaking news."""
    pool = {"a": player("a", status="Out")}
    assert news.injury_changes(pool, snap(), mine=set(), week=5) == []


def test_someone_elses_questionable_player_is_not_news():
    """Half the league is Questionable by Friday."""
    pool = {"a": player("a", status="Questionable")}
    got = news.injury_changes(pool, snap({"z": "Out"}), mine=set(), week=5)
    assert got == []


def test_someone_elses_player_being_ruled_out_is_news():
    pool = {"a": player("a", status="Out", pts=12.0)}
    got = news.injury_changes(pool, snap({"z": "Out"}), mine=set(), week=5)
    assert len(got) == 1 and not got[0].urgent


def test_your_own_players_downgrade_is_always_urgent():
    pool = {"a": player("a", status="Questionable")}
    got = news.injury_changes(pool, snap({"z": "Out"}), mine={"a"}, week=5)
    assert len(got) == 1 and got[0].urgent


def test_an_irrelevant_players_injury_is_ignored():
    pool = {"a": player("a", status="Out", pts=0.5)}
    assert news.injury_changes(pool, snap({"z": "Out"}), set(), 5) == []


# ---- who is next up ----------------------------------------------------

def test_the_next_man_up_is_the_next_free_agent_on_the_depth_chart():
    starter = player("s", depth=1, status="Out", name="Starter")
    pool = {
        "s": starter,
        "b2": player("b2", depth=2, name="Backup"),
        "b3": player("b3", depth=3, name="Third"),
    }
    assert news._next_man_up(starter, pool, owned={"s"}).player_id == "b2"


def test_a_backup_someone_already_owns_is_skipped():
    starter = player("s", depth=1, status="Out")
    pool = {"s": starter, "b2": player("b2", depth=2), "b3": player("b3", depth=3)}
    assert news._next_man_up(starter, pool, owned={"s", "b2"}).player_id == "b3"


def test_an_injured_backup_is_not_the_answer():
    starter = player("s", depth=1, status="Out")
    pool = {"s": starter, "b2": player("b2", depth=2, status="IR"),
            "b3": player("b3", depth=3)}
    assert news._next_man_up(starter, pool, owned={"s"}).player_id == "b3"


def test_a_different_position_is_not_a_replacement():
    starter = player("s", pos="RB", depth=1, status="Out")
    pool = {"s": starter, "wr": player("wr", pos="WR", depth=2)}
    assert news._next_man_up(starter, pool, owned={"s"}) is None


def test_a_different_team_is_not_a_replacement():
    starter = player("s", team="NYJ", depth=1, status="Out")
    pool = {"s": starter, "other": player("other", team="NE", depth=2)}
    assert news._next_man_up(starter, pool, owned={"s"}) is None


def test_without_a_depth_chart_the_best_projection_wins():
    starter = player("s", status="Out")
    pool = {"s": starter, "a": player("a", pts=3.0), "b": player("b", pts=9.0)}
    assert news._next_man_up(starter, pool, owned={"s"}).player_id == "b"


# ---- promotions --------------------------------------------------------

def rosters_with(mine, theirs=()):
    return [
        Roster(roster_id=1, owner_id="u1", owner_name="You", player_ids=list(mine)),
        Roster(roster_id=2, owner_id="u2", owner_name="Them", player_ids=list(theirs)),
    ]


def test_a_starter_going_down_names_the_free_agent_behind_him():
    starter = player("s", depth=1, status="Out", pts=14.0, name="Starter")
    pool = {"s": starter, "b2": player("b2", depth=2, pts=4.0, name="Backup")}
    lw = league(rosters_with(mine=[], theirs=["s"]))
    got = news.promotions(lw, pool, snap({"s": "Questionable"}), week=5)
    assert len(got) == 1
    assert "Backup" in got[0].headline and got[0].player_id == "b2"


def test_an_injury_you_already_heard_about_is_not_repeated():
    starter = player("s", depth=1, status="Out", pts=14.0)
    pool = {"s": starter, "b2": player("b2", depth=2)}
    lw = league(rosters_with(mine=[], theirs=["s"]))
    assert news.promotions(lw, pool, snap({"s": "Out"}), week=5) == []


def test_a_cold_scan_still_shows_standing_opportunities():
    """`ffm news` has no baseline, and should still be worth running."""
    starter = player("s", depth=1, status="Out", pts=14.0)
    pool = {"s": starter, "b2": player("b2", depth=2)}
    lw = league(rosters_with(mine=[], theirs=["s"]))
    assert len(news.promotions(lw, pool, snap(), week=5)) == 1


def test_your_own_players_handcuff_is_flagged_urgent():
    starter = player("s", depth=1, status="Out", pts=14.0)
    pool = {"s": starter, "b2": player("b2", depth=2)}
    lw = league(rosters_with(mine=["s"]))
    got = news.promotions(lw, pool, snap({"s": "Questionable"}), week=5)
    assert got[0].urgent
    assert any("handcuff" in d for d in got[0].detail)


def test_a_backup_nobody_can_add_is_not_suggested():
    starter = player("s", depth=1, status="Out", pts=14.0)
    pool = {"s": starter, "b2": player("b2", depth=2)}
    lw = league(rosters_with(mine=["b2"], theirs=["s"]))
    assert news.promotions(lw, pool, snap({"s": "Questionable"}), week=5) == []


# ---- the add market ----------------------------------------------------

def test_a_market_spike_needs_a_baseline_to_compare_against():
    pool = {"a": player("a")}
    lw = league(rosters_with(mine=[]))
    assert news.market_spikes(lw, pool, {"a": 90000}, snap(), 5) == []


def test_a_sudden_rush_of_adds_is_reported():
    pool = {"a": player("a", name="Riser")}
    lw = league(rosters_with(mine=[]))
    got = news.market_spikes(lw, pool, {"a": 90000},
                             snap({"z": "Out"}, {"a": 1000}), 5)
    assert len(got) == 1 and "Riser" in got[0].headline


def test_a_gentle_drift_upward_is_not_a_spike():
    pool = {"a": player("a")}
    lw = league(rosters_with(mine=[]))
    got = news.market_spikes(lw, pool, {"a": 1500},
                             snap({"z": "Out"}, {"a": 1000}), 5)
    assert got == []


def test_a_player_nobody_can_add_is_not_reported():
    pool = {"a": player("a")}
    lw = league(rosters_with(mine=["a"]))
    got = news.market_spikes(lw, pool, {"a": 90000},
                             snap({"z": "Out"}, {"a": 100}), 5)
    assert got == []


# ---- snapshot ----------------------------------------------------------

def test_a_snapshot_round_trips_through_json():
    pool = {"a": player("a", status="Out"), "b": player("b")}
    s = news.take_snapshot(pool, {"a": 5})
    assert s.injuries == {"a": "Out"}   # healthy players are not stored
    assert news.Snapshot.from_json(s.to_json()).injuries == {"a": "Out"}


def test_an_empty_snapshot_knows_it_is_empty():
    assert news.Snapshot().empty
    assert not snap({"a": "Out"}).empty


# ---- what the job was worth BEFORE it came free -----------------------
# Measuring a vacated role by the injured player's current projection is
# circular: ruling him out collapses it to zero, so every job looks worthless
# at exactly the moment it opens.

def test_the_pre_injury_value_is_remembered_not_overwritten():
    healthy = {"s": player("s", pts=14.0, depth=1)}
    snap1 = news.take_snapshot(healthy, {})
    assert snap1.values["s"] == 14.0

    # He is ruled out, so Sleeper drops his projection to nothing.
    hurt = {"s": player("s", pts=0.0, depth=1, status="Out")}
    snap2 = news.take_snapshot(hurt, {}, prev=snap1)
    assert snap2.worth_before("s") == 14.0, "the job he held must survive his injury"


def test_a_third_stringer_going_down_is_not_an_opportunity():
    """A depth-chart rank is not a job; this one scored nothing to begin with."""
    starter = player("s", depth=1, status="Out", pts=0.0)
    pool = {"s": starter, "b2": player("b2", depth=2)}
    lw = league(rosters_with(mine=[], theirs=["s"]))
    prev = snap({"s": "Questionable"}, {})
    prev.values["s"] = 0.4          # he was worth nothing before he was hurt
    assert news.promotions(lw, pool, prev, week=5) == []


def test_a_real_starter_going_down_still_reports_despite_a_zeroed_projection():
    starter = player("s", depth=1, status="Out", pts=0.0)
    pool = {"s": starter, "b2": player("b2", depth=2, name="Heir")}
    lw = league(rosters_with(mine=[], theirs=["s"]))
    prev = snap({"s": "Questionable"}, {})
    prev.values["s"] = 14.0         # he was a real starter last time we looked
    got = news.promotions(lw, pool, prev, week=5)
    assert len(got) == 1 and "Heir" in got[0].headline


def test_values_round_trip_through_json():
    s = news.Snapshot(values={"a": 12.5})
    assert news.Snapshot.from_json(s.to_json()).worth_before("a") == 12.5


# ---- recoveries --------------------------------------------------------

def test_your_player_cleared_from_out_is_news():
    """Week 4: Evans came back from Out and nobody told you."""
    pool = {"evans": player("evans", status=None, name="Mike Evans")}
    [item] = news.injury_changes(pool, snap({"evans": "Out"}), mine={"evans"}, week=4)
    assert "now active (was Out)" in item.headline
    assert not item.urgent


def test_a_return_to_questionable_says_it_is_a_game_time_call():
    pool = {"coker": player("coker", status="Questionable")}
    [item] = news.injury_changes(pool, snap({"coker": "Out"}), mine={"coker"}, week=4)
    assert "check inactives" in " ".join(item.detail)


def test_someone_elses_recovery_is_not_news():
    pool = {"a": player("a", status=None)}
    assert news.injury_changes(pool, snap({"a": "Out"}), mine=set(), week=4) == []


def test_a_questionable_tag_clearing_is_not_news():
    """Only a return from a sidelining tag reopens a lineup spot."""
    pool = {"a": player("a", status=None)}
    assert news.injury_changes(pool, snap({"a": "Questionable"}), mine={"a"}, week=4) == []


def test_out_again_after_a_recovery_is_reported_again(monkeypatch):
    """Coker's week: Questionable Wednesday, Out, back to Questionable Sunday,
    Out again Sunday night. The second ruling-out must not be deduped."""
    def change(day, before, after):
        monkeypatch.setattr(news, "_today", lambda: day)
        [item] = news.injury_changes({"c": player("c", status=after)},
                                     snap({"c": before}), {"c"}, 4)
        return item
    seq = [change("2026-09-30", "Questionable", "Out"),
           change("2026-10-04", "Out", "Questionable"),
           change("2026-10-04", "Questionable", "Out")]
    assert len({i.key for i in seq}) == 3
    assert seq[2].urgent
