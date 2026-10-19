"""The weekly report: alerts, start/sit, and the waiver board.

The draft dashboard has to be read in two seconds under a pick clock. This is
read over coffee on Sunday morning, so it optimizes for something different:
every recommendation carries the reason it was made, because a start/sit call
you do not understand is one you cannot overrule.
"""

from __future__ import annotations

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ffm.engine.startsit import WeekPlan
from ffm.engine.waivers import WaiverBoard
from ffm.models import Player

POS_STYLE = {
    "QB": "bright_magenta", "RB": "bright_green", "WR": "bright_cyan",
    "TE": "bright_yellow", "K": "grey62", "DEF": "bright_blue",
}

KIND_STYLE = {"upgrade": "bold green", "breakout": "bold yellow", "stash": "dim"}


def _pos(p: Player) -> Text:
    return Text(p.position, style=POS_STYLE.get(p.position, "white"))


def _matchup(p: Player) -> str:
    if not p.opponent:
        return "BYE"
    return f"{'vs' if p.home else '@'} {p.opponent}"


def _delta(adj) -> Text:
    d = adj.delta
    if abs(d) < 0.05:
        return Text("—", style="dim")
    return Text(f"{d:+.1f}", style="green" if d > 0 else "red")


def header(plan: WeekPlan, league_name: str) -> Panel:
    line = Text()
    line.append(f"Week {plan.week}", style="bold white")
    line.append(f"   {plan.roster.owner_name}", style="bold cyan")
    if plan.roster.wins or plan.roster.losses:
        line.append(f"  ({plan.roster.record})", style="dim")
    if plan.opponent:
        line.append("   vs ", style="dim")
        line.append(plan.opponent.owner_name, style="white")
        margin = plan.followed_raw - plan.opponent_raw
        line.append(
            f"   projected {plan.followed_raw:.1f} – {plan.opponent_raw:.1f}"
            f"  (model {plan.followed_points:.1f} – {plan.opponent_points:.1f})",
            style="dim",
        )
        line.append(
            f"  ({'+' if margin >= 0 else ''}{margin:.1f})",
            style="green" if margin >= 0 else "red",
        )
    sub = Text(f"{league_name}  ·  {plan.season}  ·  sleeper", style="dim")
    return Panel(Group(line, sub), border_style="blue", padding=(0, 1))


def alerts_panel(plan: WeekPlan) -> Panel | None:
    if not plan.alerts:
        return None
    body = Text()
    for i, a in enumerate(plan.alerts):
        if i:
            body.append("\n")
        body.append("  ●  " if a.level == "urgent" else "  ○  ",
                    style="bold red" if a.level == "urgent" else "yellow")
        body.append(a.text, style="bold white" if a.level == "urgent" else "white")
    urgent = any(a.level == "urgent" for a in plan.alerts)
    return Panel(
        body,
        title="[bold]Fix these first[/bold]",
        border_style="red" if urgent else "yellow",
        padding=(0, 1),
    )


def swaps_panel(plan: WeekPlan) -> Panel:
    if not plan.swaps:
        return Panel(
            Text("Your lineup is already the best one available. Nothing to change.",
                 style="green"),
            title="[bold]Start / sit[/bold]", border_style="green", padding=(0, 1),
        )

    body = Text()
    for i, s in enumerate(plan.swaps):
        if i:
            body.append("\n\n")
        body.append("  START  ", style="bold black on bright_green")
        body.append(f" {s.start.name} ", style="bold white")
        body.append(f"{s.start.position}-{s.start.team or 'FA'} {_matchup(s.start)}",
                    style=POS_STYLE.get(s.start.position, "white"))
        if s.sit:
            body.append("\n  over   ", style="dim")
            body.append(f" {s.sit.name} ", style="strike white")
            body.append(f"{s.sit.position}-{s.sit.team or 'FA'} {_matchup(s.sit)}",
                        style="dim")
        body.append(f"\n         in {s.slot}", style="dim")
        body.append(f"   +{s.gain:.1f} pts", style="bold green")
        for w in s.why[:4]:
            body.append(f"\n           · {w}", style="dim italic")
    body.append(
        f"\n\n  Total: {plan.current_points:.1f} → {plan.recommended_points:.1f} "
        f"(+{plan.gain:.1f})", style="bold green",
    )
    return Panel(body, title="[bold]Start / sit[/bold]", border_style="green",
                 padding=(0, 1))


def lineup_table(plan: WeekPlan) -> Table:
    t = Table(title="Recommended lineup", title_style="bold", box=None,
              padding=(0, 1))
    t.add_column("Slot", style="dim", width=10, no_wrap=True)
    t.add_column("Player", width=21, overflow="ellipsis", no_wrap=True)
    t.add_column("", width=3)
    t.add_column("Matchup", style="dim", width=7, no_wrap=True)
    t.add_column("Proj", justify="right", width=5)
    t.add_column("Adj", justify="right", width=5)
    t.add_column("Why", style="dim italic", max_width=54,
                 overflow="ellipsis", no_wrap=True)

    for slot, call in plan.by_slot():
        if call is None:
            t.add_row(slot, Text("— empty —", style="bold red"), "", "", "", "", "")
            continue
        p = call.player
        name = Text(p.name, style="bold" if not call.starting_now else "white")
        if not call.starting_now:
            name.append("  NEW", style="bold green")
        t.add_row(
            slot, name, _pos(p), _matchup(p),
            f"{p.proj_points:.1f}", _delta(call.adj),
            call.adj.summary(2),
        )
    return t


def bench_table(plan: WeekPlan, limit: int = 8) -> Table:
    t = Table(title="Bench", title_style="bold dim", box=None, padding=(0, 1))
    t.add_column("Player", width=21, overflow="ellipsis", style="dim", no_wrap=True)
    t.add_column("", width=3)
    t.add_column("Matchup", style="dim", width=7, no_wrap=True)
    t.add_column("Proj", justify="right", width=5, style="dim")
    t.add_column("Adj", justify="right", width=5)
    t.add_column("Why", style="dim italic", max_width=54,
                 overflow="ellipsis", no_wrap=True)
    for call in plan.bench[:limit]:
        p = call.player
        label = Text(p.name)
        if call.starting_now:
            label.append("  SIT", style="bold red")
        t.add_row(label, _pos(p), _matchup(p), f"{p.proj_points:.1f}",
                  _delta(call.adj), call.adj.summary(2))
    return t


def waiver_table(board: WaiverBoard, limit: int = 10) -> Table:
    t = Table(title=f"Waiver targets  ·  valued over the next {board.horizon_weeks} weeks",
              title_style="bold", box=None, padding=(0, 1))
    t.add_column("Add", width=20, overflow="ellipsis", no_wrap=True)
    t.add_column("", width=3)
    t.add_column("Matchup", style="dim", width=7, no_wrap=True)
    t.add_column("Type", width=8, no_wrap=True)
    t.add_column("Pts/wk", justify="right", width=6)
    t.add_column("Net", justify="right", width=6)
    t.add_column("Verdict", width=15, no_wrap=True)
    t.add_column("Drop", width=16, overflow="ellipsis", style="dim", no_wrap=True)
    t.add_column("Why", style="dim italic", max_width=46,
                 overflow="ellipsis", no_wrap=True)

    verdict_style = {
        "add": "bold green", "worth_it": "green", "marginal": "yellow",
        "speculative": "yellow", "skip": "dim red", "hold": "dim",
    }
    for p in board.pickups[:limit]:
        m = p.move
        net = m.net if m else p.horizon_gain
        t.add_row(
            Text(p.player.name, style="bold"),
            _pos(p.player),
            _matchup(p.player),
            Text(p.kind, style=KIND_STYLE.get(p.kind, "white")),
            f"{p.horizon_points:.1f}",
            Text(f"{net:+.1f}", style="green" if net > 0 else "red"),
            Text(m.summary if m else "", style=verdict_style.get(
                m.verdict if m else "", "white")),
            (p.drop.name if p.drop else
             (f"— {m.open_spots} spare" if m and m.open_spots else "")),
            "; ".join(p.why[:2]),
        )
    return t


def _footnotes(plan: WeekPlan | None, board: WaiverBoard | None) -> Text | None:
    stale = (plan and plan.stale_context) or (board and board.stale_context)
    if not stale:
        return None
    return Text(
        "Context signals are drawn from last season — this season has no completed "
        "games yet, so team form and matchup data are weighted at half strength "
        "and rosters have changed. Your opinions.toml carries more weight than "
        "usual right now.",
        style="dim yellow",
    )


def brief_panel(brief) -> Panel:
    body = Text(brief.text, style="italic")
    tag = f"{brief.model}{' · cached' if brief.cached else ''}"
    return Panel(body, title="[bold]The short version[/bold]",
                 subtitle=f"[dim]{tag}[/dim]", border_style="magenta",
                 padding=(0, 1))


def print_week(plan: WeekPlan, league_name: str, console: Console,
               board: WaiverBoard | None = None, show_bench: bool = True,
               brief=None) -> None:
    console.print(header(plan, league_name))
    if brief:
        console.print(brief_panel(brief))
    alerts = alerts_panel(plan)
    if alerts:
        console.print(alerts)
    console.print(swaps_panel(plan))
    console.print(lineup_table(plan))
    if show_bench and plan.bench:
        console.print(bench_table(plan))
    if board and board.pickups:
        console.print()
        console.print(waiver_table(board))
    note = _footnotes(plan, board)
    if note:
        console.print()
        console.print(note)


NEWS_STYLE = {"injury": "red", "promotion": "bright_green", "market": "yellow"}


def news_panel(items) -> Panel:
    body = Text()
    for i, item in enumerate(items):
        if i:
            body.append("\n\n")
        tag = {"injury": "INJURY", "promotion": "PICKUP", "market": "TRENDING"}
        body.append(f"  {tag.get(item.kind, 'NEWS'):9s}",
                    style=f"bold {NEWS_STYLE.get(item.kind, 'white')}")
        body.append(item.headline, style="bold white" if item.urgent else "white")
        for d in item.detail:
            body.append(f"\n            {d}", style="dim")
        move = getattr(item, "move", None)
        if move:
            body.append(f"\n            -> {move.summary}",
                        style="bold green" if move.do_it else "yellow")
            for l in move.lines:
                body.append(f"\n               {l}", style="dim")
    urgent = any(i.urgent for i in items)
    return Panel(body, title="[bold]What just changed[/bold]",
                 border_style="red" if urgent else "green", padding=(0, 1))


def print_news(items, league_name: str, week: int, console: Console) -> None:
    console.print(Panel(Text(f"Week {week}  ·  {league_name}", style="bold white"),
                        border_style="blue", padding=(0, 1)))
    if not items:
        console.print(Text("No injuries, promotions or market moves to report.",
                           style="green"))
        return
    console.print(news_panel(items))


def print_waivers(board: WaiverBoard, league_name: str, console: Console,
                  limit: int = 12) -> None:
    console.print(Panel(
        Text(f"Week {board.week}  ·  {league_name}", style="bold white"),
        border_style="blue", padding=(0, 1),
    ))
    if not board.pickups:
        console.print(Text("Nothing on waivers beats what you already have.",
                           style="green"))
    else:
        console.print(waiver_table(board, limit=limit))
        console.print()
        t = Table(title="Droppable, worst first", title_style="bold dim", box=None,
                  padding=(0, 1))
        t.add_column("Player", width=21, style="dim", no_wrap=True,
                     overflow="ellipsis")
        t.add_column("", width=3)
        t.add_column("Pts/wk", justify="right", width=6, style="dim")
        t.add_column("Note", style="yellow italic")
        for d in board.drops:
            t.add_row(d.player.name, _pos(d.player),
                      f"{d.horizon_points:.1f}", d.note)
        console.print(t)
    note = _footnotes(None, board)
    if note:
        console.print()
        console.print(note)
