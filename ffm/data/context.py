"""The gut check: everything the projection does not know.

A weekly projection is a points estimate built mostly from a player's own
recent box scores. It is good at "how many yards has he been getting" and bad
at the questions that decide real lineups: is this offense actually any good,
is he still the guy getting the ball, and who is he playing this week. A team
can look fine on paper and be a team you do not want to start anybody from.

So four signals are measured alongside the projection, each derived from data
rather than opinion and each required to explain itself in a sentence that
ends up on screen:

* **Team reality** — actual points scored by a team's players against what was
  projected for those same players, week by week. A team the model keeps
  overrating shows up here as a persistent shortfall. This is the direct
  measurement of "good on paper".
* **Matchup** — fantasy points each defense has actually allowed to each
  position, against the league average.
* **Role** — snap, target and carry share, recent games against the earlier
  baseline. Usage moves before points do, so this catches the promotion or the
  benching while the projection is still averaging over old games.
* **Availability** — an injury tag is a discount, not a footnote.

Plus your own read, from `opinions.toml`, which outranks all of them: when you
know something about a team that the box scores have not caught up to yet,
that belongs in the model, not in your head.

Only availability and your own read move the number. Team, matchup and role
are reported, not priced: backtested over 2,156 player-games (2025 weeks 8-17,
2026 weeks 3-5), each one made the projection worse on its own, and together
they called 54.9% of close same-position start/sits right against 56.8% for
the bare projection. The projection already reads recent box scores, usage and
the opponent, so pricing them again counted them twice. They stay as the
reasons beside a call, sized and signed as before, so you still see that a
receiver has a tough matchup — it just no longer bends his number.

Every signal is shrunk toward "no opinion" by how much data actually backs it,
and the total is clamped.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ffm.data import weekly
from ffm.models import SKILL_POSITIONS, LeagueSettings, Player

# Per-signal ceilings, as a fraction of a player's projection.
_CAP = {"team": 0.10, "matchup": 0.12, "role": 0.10, "you": 0.35}

# Total swing this layer may apply. Wide enough to flip a close call, narrow
# enough that it can never manufacture a start out of a bad player.
_TOTAL_CAP = 0.30

# Shrinkage constants: a signal backed by `n` observations is trusted
# n / (n + k) of the way. Bigger k = more evidence demanded.
_K_TEAM = 3.0
_K_DEF = 4.0
_K_ROLE = 2.0

# A projection this small is noise; percentage swings on it are meaningless.
_MIN_PROJ = 2.0

_INJURY_DISCOUNT = {
    "IR": 1.0, "Out": 1.0, "PUP": 1.0, "Sus": 1.0,
    "Doubtful": 0.60, "Questionable": 0.10,
}

# A team-level opinion is a claim about an offense, so it moves the players who
# share in that offense. It deliberately stops short of K and DEF: a kicker's
# value partly *inverts* with offensive quality — drives that stall inside the
# 40 become field goal attempts — and a team defense scores off turnovers and
# sacks, which have little to do with its own offense being good. Name the
# kicker or the defense directly under [players] to move it anyway, or set
# `positions` on the team entry.
TEAM_OPINION_POSITIONS = ("QB", "RB", "WR", "TE")

OPINION_LOCATIONS = (
    Path.cwd() / "opinions.toml",
    Path.home() / ".config" / "ff-manager" / "opinions.toml",
)


# The signals that move a projection. The rest are context: shown, not priced.
PRICED = frozenset({"health", "you"})


@dataclass(frozen=True)
class Signal:
    kind: str      # team | matchup | role | health | you
    delta: float   # fractional lean, e.g. -0.08; applied only if `priced`
    note: str      # why, in words, for the report

    @property
    def priced(self) -> bool:
        return self.kind in PRICED


@dataclass
class Adjustment:
    """What the context layer did to one player's projection, and why."""

    base: float
    multiplier: float
    signals: list[Signal] = field(default_factory=list)

    @property
    def points(self) -> float:
        return round(self.base * self.multiplier, 2)

    @property
    def delta(self) -> float:
        return round(self.points - self.base, 2)

    @property
    def notes(self) -> list[str]:
        return [s.note for s in self.ranked]

    @property
    def ranked(self) -> list[Signal]:
        """Signals strongest-first, so a truncated list keeps what mattered."""
        return sorted(self.signals, key=lambda s: -abs(s.delta))

    def top_notes(self, n: int = 2) -> list[str]:
        return [s.note for s in self.ranked[:n]]

    def multiplier_excluding(self, *kinds: str) -> float:
        """The same adjustment with some signals left out.

        Waiver value is a multi-week question, so the matchup term — which is
        about one Sunday — has no business in it.
        """
        return combine(s for s in self.signals if s.kind not in kinds)

    def summary(self, n: int = 2) -> str:
        return "; ".join(self.top_notes(n))


# ---- your own read -----------------------------------------------------

@dataclass(frozen=True)
class Nudge:
    """One hand-written adjustment."""

    adjust: float
    why: str = ""
    # None means the default set for its section. Only meaningful for teams;
    # naming a player is already specific enough to mean what it says.
    positions: tuple[str, ...] | None = None

    def covers(self, position: str) -> bool:
        # `None` means "not specified, use the default set"; an empty tuple
        # means "specified, and nothing valid survived" — which must cover
        # nothing. Folding those together with `or` made a typo'd positions
        # list silently apply to every position instead of none.
        allowed = TEAM_OPINION_POSITIONS if self.positions is None else self.positions
        return position in allowed


@dataclass
class Opinions:
    """Hand-written adjustments. Yours outrank every derived signal."""

    teams: dict[str, Nudge] = field(default_factory=dict)
    players: dict[str, Nudge] = field(default_factory=dict)
    source: Path | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "Opinions":
        paths = [path] if path else list(OPINION_LOCATIONS)
        for p in paths:
            if p and p.exists():
                try:
                    raw = tomllib.loads(p.read_text())
                except (tomllib.TOMLDecodeError, OSError):
                    # OSError: a bind mount whose host file is missing comes
                    # up as a directory, and that must not stop the watcher.
                    continue
                return cls(
                    teams=_parse_opinions(raw.get("teams") or {}),
                    players=_parse_opinions(raw.get("players") or {}),
                    source=p,
                )
        return cls()

    def for_player(self, player: Player) -> list[Signal]:
        out: list[Signal] = []
        named = self.players.get(_norm(player.name))
        if named:
            # Naming someone is unambiguous, so position never filters it.
            out.append(Signal("you", named.adjust,
                              named.why or f"your call on {player.name}"))
        team = self.teams.get((player.team or "").upper())
        if team and team.covers(player.position):
            out.append(Signal("you", team.adjust,
                              team.why or f"your call on {player.team}"))
        return out


def _parse_opinions(section: dict) -> dict[str, Nudge]:
    """Accept `NYJ = -0.15` or `NYJ = {adjust = -0.15, why = "...", positions = [...]}`."""
    out: dict[str, Nudge] = {}
    for key, val in section.items():
        positions: tuple[str, ...] | None = None
        if isinstance(val, dict):
            adj = float(val.get("adjust") or 0.0)
            why = str(val.get("why") or "")
            raw = val.get("positions")
            if isinstance(raw, list):
                # Unknown position names are dropped rather than raised: a typo
                # should not take the whole report down, and an entry that
                # covers nothing simply does nothing.
                kept = tuple(
                    str(p).upper() for p in raw
                    if str(p).upper() in SKILL_POSITIONS
                )
                positions = kept
        else:
            try:
                adj, why = float(val), ""
            except (TypeError, ValueError):
                continue
        k = key.upper() if len(key) <= 4 and key.isalpha() else _norm(key)
        out[k] = Nudge(
            adjust=max(-_CAP["you"], min(_CAP["you"], adj)),
            why=why,
            positions=positions,
        )
    return out


def _norm(name: str) -> str:
    from ffm.models import normalize_name
    return normalize_name(name)


# ---- the engine --------------------------------------------------------

@dataclass
class TeamForm:
    ratio: float   # actual points / projected points
    games: float   # weighted games behind it


class ContextEngine:
    """Builds the four derived signals once, then prices any player quickly."""

    def __init__(
        self,
        settings: LeagueSettings,
        season: str,
        week: int,
        opinions: Opinions | None = None,
        lookback: int = 5,
    ):
        self.settings = settings
        self.season = season
        self.week = week
        self.lookback = lookback
        self.opinions = opinions if opinions is not None else Opinions.load()

        self.team_form: dict[str, TeamForm] = {}
        self.def_allowed: dict[tuple[str, str], tuple[float, float]] = {}
        self.pos_baseline: dict[str, float] = {}
        self.role: dict[str, dict[str, float]] = {}
        self.window: list[tuple[str, int, float]] = []
        self.stale = False  # window leans on last season

    # -- construction ---------------------------------------------------

    def build(self) -> "ContextEngine":
        self.window = weekly.history_window(self.season, self.week, self.lookback)
        self.stale = all(s != str(self.season) for s, _, _ in self.window)
        meta = _player_meta()

        team_act: dict[str, float] = {}
        team_proj: dict[str, float] = {}
        team_games: dict[str, float] = {}
        allowed: dict[tuple[str, str], float] = {}
        def_games: dict[str, float] = {}
        usage: dict[str, list[tuple[int, dict[str, float], bool]]] = {}
        teams_played: dict[int, set[str]] = {}

        for order, (season, wk, weight) in enumerate(self.window):
            try:
                proj = weekly.scored_week(season, wk, self.settings, actual=False)
                act = weekly.scored_week(season, wk, self.settings, actual=True)
                use = weekly.usage_week(season, wk)
            except Exception:
                continue
            opp = weekly.opponents(season).get(wk, {})
            teams_played[order] = set(opp)

            current = season == str(self.season)
            for pid, played in use.items():
                usage.setdefault(pid, []).append((order, played, current))

            # Byes mean a team plays fewer games than the window is long, so
            # per-game rates have to divide by games actually played.
            for team in opp:
                team_games[team] = team_games.get(team, 0.0) + weight
                def_games[team] = def_games.get(team, 0.0) + weight

            for pid, actual in act.items():
                m = meta.get(pid)
                if not m or pid not in use:
                    continue  # took no snaps: an availability story, not a team one
                team, pos = m.get("team"), m.get("position")
                if not team or pos not in ("QB", "RB", "WR", "TE"):
                    continue
                expected = proj.get(pid, 0.0)
                if expected >= _MIN_PROJ:
                    team_act[team] = team_act.get(team, 0.0) + actual * weight
                    team_proj[team] = team_proj.get(team, 0.0) + expected * weight
                matchup = opp.get(team)
                if matchup:
                    # Weighted alongside def_games so the ratio stays a true
                    # per-game rate while last season counts for less.
                    allowed[(matchup[0], pos)] = (
                        allowed.get((matchup[0], pos), 0.0) + actual * weight
                    )

        for team, proj_pts in team_proj.items():
            if proj_pts > 0:
                self.team_form[team] = TeamForm(
                    ratio=team_act.get(team, 0.0) / proj_pts,
                    games=team_games.get(team, 0.0),
                )

        for (team, pos), total in allowed.items():
            g = def_games.get(team, 0.0)
            if g > 0:
                self.def_allowed[(team, pos)] = (total / g, g)
        for pos in {p for (_, p) in self.def_allowed}:
            rates = [v for (t, p), (v, _) in self.def_allowed.items() if p == pos]
            self.pos_baseline[pos] = sum(rates) / len(rates) if rates else 0.0

        self.role = {}
        for pid, rows in usage.items():
            m = meta.get(pid) or {}
            team = m.get("team")
            played = {r[0] for r in rows}
            missed = {o for o, teams in teams_played.items()
                      if team in teams and o not in played}
            tagged = m.get("injury_status") in _INJURY_DISCOUNT
            self.role[pid] = _role_trend(_drop_injury_exits(rows, missed, tagged))
        return self

    # -- pricing --------------------------------------------------------

    def adjust(self, player: Player, base: float | None = None) -> Adjustment:
        base = player.proj_points if base is None else base
        signals: list[Signal] = []
        if base < _MIN_PROJ:
            return Adjustment(base=base, multiplier=1.0, signals=signals)

        signals.extend(self._team_signal(player))
        signals.extend(self._matchup_signal(player))
        signals.extend(self._role_signal(player))
        signals.extend(self._health_signal(player))
        signals.extend(self.opinions.for_player(player))

        return Adjustment(base=base, multiplier=combine(signals), signals=signals)

    # -- individual signals ---------------------------------------------

    def _team_signal(self, player: Player) -> list[Signal]:
        form = self.team_form.get(player.team or "")
        if not form or player.position in ("K", "DEF"):
            return []
        trust = form.games / (form.games + _K_TEAM)
        delta = _clamp((form.ratio - 1.0) * trust, _CAP["team"])
        if abs(delta) < 0.025:
            return []
        pct = abs(form.ratio - 1.0) * 100
        verb = "over" if form.ratio > 1 else "under"
        era = " last season" if self.stale else ""
        return [Signal(
            "team",
            delta,
            f"{player.team} has been {verb}performing its projections by "
            f"{pct:.0f}%{era}",
        )]

    def _matchup_signal(self, player: Player) -> list[Signal]:
        if not player.opponent or player.position not in ("QB", "RB", "WR", "TE"):
            return []
        entry = self.def_allowed.get((player.opponent, player.position))
        baseline = self.pos_baseline.get(player.position, 0.0)
        if not entry or baseline <= 0:
            return []
        per_game, n = entry
        trust = n / (n + _K_DEF)
        delta = _clamp((per_game / baseline - 1.0) * trust, _CAP["matchup"])
        if abs(delta) < 0.015:
            return []
        kind = "soft" if delta > 0 else "tough"
        return [Signal(
            "matchup",
            delta,
            f"{kind} matchup vs {player.opponent} "
            f"({per_game:.0f} pts allowed to {player.position}s per game "
            f"vs {baseline:.0f} average)",
        )]

    def _role_signal(self, player: Player) -> list[Signal]:
        trend = self.role.get(player.player_id)
        if not trend or player.position in ("K", "DEF"):
            return []
        shift, recent, earlier, metric = role_view(trend, player.position)
        n = trend.get("games", 0.0)
        if abs(shift) < 0.06:
            return []  # ordinary week-to-week wobble, not a role change
        trust = n / (n + _K_ROLE)
        delta = _clamp(shift * 1.5 * trust, _CAP["role"])
        if abs(delta) < 0.02:
            return []
        direction = "rising" if delta > 0 else "shrinking"
        return [Signal(
            "role",
            delta,
            f"{direction} role ({earlier * 100:.0f}% -> "
            f"{recent * 100:.0f}% of {metric})",
        )]

    def _health_signal(self, player: Player) -> list[Signal]:
        """Fraction of a player's value written off for his injury tag."""
        pct = _INJURY_DISCOUNT.get(player.injury_status or "", 0.0)
        if not pct:
            return []
        return [Signal("health", -pct, f"listed {player.injury_status}")]


def role_view(trend: dict, position: str) -> tuple[float, float, float, str]:
    """Pick the usage metric that means something for this position.

    Opportunity share is the sharper signal for a back losing carries or a
    receiver losing targets. For a quarterback it is his share of the team's
    rushing attempts, which is noise — it read a normal week as Mahomes losing
    his job — so quarterbacks are judged on snaps alone.
    """
    snap = (trend.get("snap_shift", 0.0), trend.get("snap_recent", 0.0),
            trend.get("snap_earlier", 0.0), "snaps")
    if position == "QB":
        return snap
    opp = (trend.get("opp_shift", 0.0), trend.get("opp_recent", 0.0),
           trend.get("opp_earlier", 0.0), "touches")
    return opp if abs(opp[0]) > abs(snap[0]) else snap


def combine(signals) -> float:
    """Fold the priced signals into one bounded multiplier."""
    signals = [s for s in signals if s.priced]
    total = sum(s.delta for s in signals)
    total = max(-_TOTAL_CAP, min(_TOTAL_CAP, total))
    # A health discount is a hard fact about availability, not a tilt, so it
    # applies outside the tilt budget: a doubtful player must not be rescued
    # by a good matchup.
    health = min((s.delta for s in signals if s.kind == "health"), default=0.0)
    if health < 0:
        total = min(total, health)
    return 1.0 + total


def _clamp(v: float, cap: float) -> float:
    return max(-cap, min(cap, v))


def _drop_injury_exits(
    rows: list[tuple[int, dict[str, float], bool]],
    missed: set[int],
    tagged: bool,
) -> list[tuple[int, dict[str, float], bool]]:
    """Leave out games a player left hurt; they say nothing about his role.

    David Montgomery played 9 of 66 snaps in week 5 before a groin injury
    took him out. Averaged into the recent sample, that read as "shrinking
    role, 56% -> 36%" and cut him 10% for the next two games he played at a
    full workload.

    A game is an injury exit when his snap share fell under half his usual
    and he then missed his team's next game, or it is his latest game and he
    still carries an injury tag. A benching looks the same on the snap count
    but leaves him healthy and playing, so it still counts.
    """
    if len(rows) < 3:
        return rows
    latest = min(r[0] for r in rows)
    keep = []
    for row in rows:
        order, use, current = row
        others = sorted(r[1]["snap_share"] for r in rows if r is not row)
        usual = others[len(others) // 2]
        hurt = (order - 1) in missed or (order == latest and tagged)
        if current and hurt and use["snap_share"] < usual / 2:
            continue
        keep.append(row)
    return keep


def _role_trend(rows: list[tuple[int, dict[str, float], bool]]) -> dict[str, float]:
    """Recent usage against the earlier baseline, on both snaps and touches.

    `order` counts backwards from now, so the lowest orders are the most
    recent games. Split at two games: enough to catch a role change, few
    enough that one big game does not read as a promotion.

    Both recent games must be from this season. The window runs back into
    last year, and in week 2 the "last two games" were week 1 plus last
    season's week 17 — a meaningless finale where starters sit. That pairing
    read Harold Fannin (82% of snaps in week 1) as "shrinking, 91% -> 53%".
    Until a second game is played there is no recent sample, only a baseline.
    """
    rows = sorted(rows, key=lambda r: r[0])
    if not rows:
        return {}
    this_season = [r for r in rows if r[2]]
    recent_rows = this_season[:2] if len(this_season) >= 2 else []
    recent = [r[1] for r in recent_rows]
    earlier = [r[1] for r in rows[len(recent_rows):]] if recent_rows else []
    if not recent or not earlier:
        share = sum(r[1]["snap_share"] for r in rows) / len(rows)
        return {"games": float(len(rows)),
                "snap_recent": share, "snap_earlier": share, "snap_shift": 0.0,
                "opp_recent": share, "opp_earlier": share, "opp_shift": 0.0}

    def mean(vals, key):
        return sum(v[key] for v in vals) / len(vals) if vals else 0.0

    r_snap, e_snap = mean(recent, "snap_share"), mean(earlier, "snap_share")
    # Opportunity share moves earlier than snap share for a back splitting
    # carries, so take whichever shift is larger — and report the same one,
    # or the note ends up quoting a number that did not move.
    r_opp = mean(recent, "target_share") + mean(recent, "carry_share")
    e_opp = mean(earlier, "target_share") + mean(earlier, "carry_share")
    return {
        "games": float(len(rows)),
        "snap_recent": r_snap, "snap_earlier": e_snap, "snap_shift": r_snap - e_snap,
        "opp_recent": r_opp, "opp_earlier": e_opp, "opp_shift": r_opp - e_opp,
    }


_META_CACHE: dict[str, dict] | None = None


def _player_meta() -> dict[str, dict]:
    global _META_CACHE
    if _META_CACHE is None:
        from ffm.data.sleeper_data import all_players
        _META_CACHE = all_players()
    return _META_CACHE
