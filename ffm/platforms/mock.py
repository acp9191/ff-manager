"""Offline simulated draft.

Two jobs: it lets the engine be tested without a live draft, and it gives you
a dry run of the dashboard before the real thing starts.
"""

from __future__ import annotations

import random

from ffm.data import scoring as scoring_mod
from ffm.models import DraftPick, DraftState, LeagueSettings, Player
from ffm.platforms.base import DraftAdapter

DEFAULT_SLOTS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF"] + ["BN"] * 6


class MockAdapter(DraftAdapter):
    platform = "mock"

    def __init__(
        self,
        pool_provider,
        teams: int = 12,
        rounds: int = 15,
        my_slot: int = 5,
        scoring_type: str = "half_ppr",
        roster_slots: list[str] | None = None,
        seed: int = 0,
        reach: float = 0.0,
    ):
        self._settings = LeagueSettings(
            platform="mock",
            league_id="mock",
            draft_id="mock",
            name=f"Mock {teams}-team {scoring_type}",
            teams=teams,
            rounds=rounds,
            roster_slots=roster_slots or list(DEFAULT_SLOTS),
            scoring=scoring_mod.default_scoring(scoring_type),
            scoring_type=scoring_type,
            draft_type="snake",
        )
        self._pool_provider = pool_provider
        self._pool: dict[str, Player] | None = None
        self.my_slot_no = my_slot
        self.picks: list[DraftPick] = []
        self.rng = random.Random(seed)
        self.reach = reach

    def league_settings(self) -> LeagueSettings:
        return self._settings

    def pool(self) -> dict[str, Player]:
        if self._pool is None:
            self._pool = self._pool_provider(self._settings)
        return self._pool

    def fetch_state(self) -> DraftState:
        return DraftState(
            settings=self._settings,
            picks=list(self.picks),
            my_slot=self.my_slot_no,
            status="complete" if len(self.picks) >= self._settings.teams * self._settings.rounds
            else "drafting",
            slot_names={i: (f"You" if i == self.my_slot_no else f"Team {i}")
                        for i in range(1, self._settings.teams + 1)},
        )

    def resolve_player(self, raw_id: str, pool: dict[str, Player]) -> str | None:
        return raw_id if raw_id in pool else None

    # ---- simulation ---------------------------------------------------

    def simulate_until_my_turn(self, max_picks: int = 200) -> None:
        """Let the bots pick until it is my turn (or the draft ends)."""
        for _ in range(max_picks):
            state = self.fetch_state()
            if state.is_complete or state.is_my_turn():
                return
            self._bot_pick(state)

    def draft_player(self, player_id: str) -> None:
        state = self.fetch_state()
        self._append(state, player_id)

    def _append(self, state: DraftState, player_id: str | None) -> None:
        n = state.current_pick_no
        rnd, _ = state.round_and_pick(n)
        self.picks.append(
            DraftPick(pick_no=n, round=rnd, slot=state.slot_for_pick(n), player_id=player_id)
        )

    def _bot_pick(self, state: DraftState) -> None:
        """Bots draft near ADP with noise, and stop taking K/DEF early."""
        drafted = state.drafted_ids()
        rounds_left = self._settings.rounds - state.round_and_pick(state.current_pick_no)[0]
        avail = [
            p for p in self.pool().values()
            if p.player_id not in drafted and p.adp < 999
            and (p.position not in ("K", "DEF") or rounds_left <= 1)
        ]
        if not avail:
            self._append(state, None)
            return
        noise = 12.0
        best = min(avail, key=lambda p: p.adp + self.rng.gauss(self.reach, noise))
        self._append(state, best.player_id)
