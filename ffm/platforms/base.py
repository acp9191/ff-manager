"""The adapter contract every platform implements.

Keeping this narrow is what makes the future work cheap: trades, waivers and
start/sit need `league_settings()` plus a roster feed, which is the same shape
the draft path already produces.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ffm.models import DraftState, LeagueSettings, Player


class DraftAdapter(ABC):
    """Reads a live draft from a platform and normalizes it."""

    platform: str = "unknown"

    @abstractmethod
    def league_settings(self) -> LeagueSettings:
        """League/draft configuration. Cached by callers; may be slow."""

    @abstractmethod
    def fetch_state(self) -> DraftState:
        """A fresh snapshot. Called on every poll, so keep it to 1-2 requests."""

    @abstractmethod
    def resolve_player(self, raw_id: str, pool: dict[str, Player]) -> str | None:
        """Map a platform player id onto our canonical Sleeper player id."""


class AdapterError(RuntimeError):
    """Raised for auth/config problems the user can actually fix."""
