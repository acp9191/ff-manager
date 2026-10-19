"""Polls a live draft and drives the dashboard."""

from __future__ import annotations

import time
from dataclasses import dataclass

from rich.console import Console
from rich.live import Live

from ffm.data.sleeper_data import build_player_pool
from ffm.engine.recommend import Advice, analyze
from ffm.models import DraftState, Player
from ffm.platforms.base import DraftAdapter
from ffm.ui import dashboard


@dataclass
class Session:
    adapter: DraftAdapter
    pool: dict[str, Player]
    bpa_weight: float = 0.20
    _id_map: dict[str, str | None] = None  # platform id -> canonical id

    def __post_init__(self) -> None:
        self._id_map = {}

    def normalize(self, state: DraftState) -> DraftState:
        """Rewrite platform player ids to canonical Sleeper ids."""
        for pick in state.picks:
            raw = pick.player_id
            if not raw or raw in self.pool:
                continue
            if raw not in self._id_map:
                self._id_map[raw] = self.adapter.resolve_player(raw, self.pool)
            pick.player_id = self._id_map[raw] or raw
        return state

    def advice(self) -> Advice:
        state = self.normalize(self.adapter.fetch_state())
        return analyze(state, self.pool, bpa_weight=self.bpa_weight)


def build_session(adapter: DraftAdapter, bpa_weight: float = 0.20) -> Session:
    settings = adapter.league_settings()
    pool = build_player_pool(settings)
    return Session(adapter=adapter, pool=pool, bpa_weight=bpa_weight)


def unmatched_report(session: Session, state: DraftState) -> list[str]:
    """Platform picks we could not map onto a known player."""
    return [p.player_id for p in state.picks if p.player_id and p.player_id not in session.pool]


def live_loop(
    session: Session,
    console: Console,
    interval: float = 5.0,
    bell: bool = True,
    step_hook=None,
) -> None:
    """Refresh the dashboard until the draft completes or the user quits.

    `step_hook` runs once per cycle before sleeping; the mock draft uses it to
    advance the simulation.
    """
    last_pick_count = -1
    was_my_turn = False
    errors = 0

    with Live(console=console, screen=True, refresh_per_second=4, transient=False) as live:
        while True:
            try:
                advice = session.advice()
                errors = 0
            except Exception as exc:  # keep the board up through network blips
                errors += 1
                live.console.print(f"[red]poll error ({errors}): {exc}[/red]")
                if errors >= 10:
                    raise
                time.sleep(min(30, interval * errors))
                continue

            state = advice.state
            if bell and state.is_my_turn() and not was_my_turn:
                console.bell()
            was_my_turn = state.is_my_turn()

            if state.picks_made != last_pick_count:
                last_pick_count = state.picks_made
            status = (
                f"polling every {interval:g}s  ·  {state.picks_made} picks in  ·  "
                f"{time.strftime('%H:%M:%S')}  ·  Ctrl-C to exit"
            )
            live.update(dashboard.render(advice, session.pool, status))

            if state.is_complete:
                live.console.print("\n[bold green]Draft complete.[/bold green]")
                return
            if step_hook:
                step_hook(session)
            time.sleep(interval)
