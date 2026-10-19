"""Assembles the in-season pieces, the way runner.py does for the draft.

One place that knows the order things have to be built in, so the CLI stays a
thin argument parser and the engines stay ignorant of where their data came
from.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ffm.data import weekly
from ffm.data.context import ContextEngine, Opinions
from ffm.engine import startsit, waivers
from ffm.engine.roster_move import MoveAdvisor
from ffm.models import LeagueSettings, LeagueWeek, Player, Roster
from ffm.platforms.league import SleeperLeague


@dataclass
class SeasonSession:
    league: SleeperLeague
    settings: LeagueSettings
    state: LeagueWeek
    pool: dict[str, Player]
    ctx: ContextEngine
    week: int
    season: str
    horizon_weeks: int = 4
    _horizon: dict[str, float] | None = None
    _advisor: MoveAdvisor | None = None

    def opponent(self) -> Roster | None:
        rid = self.league.opponent_roster_id(self.week, self.state.my_roster_id)
        if rid is None:
            return None
        return next((r for r in self.state.rosters if r.roster_id == rid), None)

    def plan(self) -> startsit.WeekPlan:
        return startsit.plan_week(self.state, self.pool, self.ctx, self.opponent(),
                                  locked=self.locked_ids())

    def locked_ids(self) -> set[str]:
        """My players whose game this week has kicked off or finished."""
        status = weekly.game_status(self.season, self.week)
        return {
            pid for pid in self.state.me.player_ids
            if pid in self.pool
            and status.get(self.pool[pid].team or "") in weekly.LOCKED
        }

    def horizon(self) -> dict[str, float]:
        if self._horizon is None:
            self._horizon = weekly.horizon_points(
                self.settings, self.week, self.horizon_weeks, self.season
            )
        return self._horizon

    def advisor(self) -> MoveAdvisor:
        """Built once and shared, so every path gives the same drop advice."""
        if self._advisor is None:
            self._advisor = MoveAdvisor(
                self.state, self.pool, self.ctx, self.horizon(), self.horizon_weeks
            )
        return self._advisor

    def board(self, limit: int = 12) -> waivers.WaiverBoard:
        market = weekly.trending("add", hours=48, limit=120)
        return waivers.build_board(
            self.state, self.pool, self.ctx, self.horizon(), market,
            horizon_weeks=self.horizon_weeks, limit=limit,
            advisor=self.advisor(),
        )


def build_season(
    league_id: str,
    username: str | None = None,
    roster_id: int | None = None,
    week: int | None = None,
    lookback: int = 5,
    horizon_weeks: int = 4,
    opinions_path: Path | None = None,
) -> SeasonSession:
    league = SleeperLeague(league_id, username=username, roster_id=roster_id)
    settings = league.settings()
    season = league.season or weekly.current_season()
    week = week or weekly.current_week()

    state = league.week_state(week)
    pool = weekly.build_week_pool(settings, week, season)
    # Rostered players the weekly feed skips (byes, deep bench) still have to
    # appear, or the roster silently loses people.
    everyone = {p for r in state.rosters for p in r.player_ids + r.reserve}
    weekly.ensure_players(pool, everyone, week, season)

    ctx = ContextEngine(
        settings, season=season, week=week,
        opinions=Opinions.load(opinions_path), lookback=lookback,
    ).build()

    return SeasonSession(
        league=league, settings=settings, state=state, pool=pool, ctx=ctx,
        week=week, season=season, horizon_weeks=horizon_weeks,
    )
