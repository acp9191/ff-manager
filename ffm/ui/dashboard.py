"""The live draft dashboard.

Optimized for one thing: during a 90-second pick clock, the answer should be
readable in about two seconds. The recommendation is the largest element on
screen; everything else supports the decision behind it.
"""

from __future__ import annotations

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ffm.engine.recommend import Advice
from ffm.models import Player

POS_STYLE = {
    "QB": "bright_magenta", "RB": "bright_green", "WR": "bright_cyan",
    "TE": "bright_yellow", "K": "grey62", "DEF": "bright_blue",
}


def _pos(p: Player) -> Text:
    return Text(p.position, style=POS_STYLE.get(p.position, "white"))


def _survival_text(s: float) -> Text:
    pct = f"{s * 100:3.0f}%"
    style = "bold red" if s < 0.25 else "yellow" if s < 0.6 else "green"
    return Text(pct, style=style)


def header(advice: Advice) -> Panel:
    st = advice.state
    s = st.settings
    if st.is_complete:
        line = Text("DRAFT COMPLETE", style="bold green")
    elif not st.has_started:
        line = Text()
        line.append("WAITING FOR DRAFT TO START", style="bold yellow")
        if advice.this_pick:
            rnd, pk = st.round_and_pick(advice.this_pick)
            line.append(f"   you pick {rnd}.{pk:02d} (#{advice.this_pick})", style="cyan")
    else:
        rnd, pk = st.round_and_pick(st.current_pick_no)
        otc = st.on_the_clock_slot()
        mine = st.is_my_turn()
        who = "YOU" if mine else st.slot_names.get(otc, f"Team {otc}")
        line = Text()
        line.append(f"Pick {rnd}.{pk:02d}  (#{st.current_pick_no})   ", style="bold white")
        line.append("ON THE CLOCK: ", style="dim")
        line.append(who, style="bold black on bright_yellow" if mine else "bold white")
        if advice.this_pick and not mine:
            line.append(f"   your next: #{advice.this_pick}", style="cyan")
            line.append(f"  (in {advice.this_pick - st.current_pick_no} picks)", style="dim cyan")

    sub = Text(
        f"{s.name}  ·  {s.teams}-team {s.draft_type}  ·  {s.scoring_type}"
        f"{'  ·  SUPERFLEX' if s.is_superflex else ''}  ·  slot {st.my_slot}  ·  {s.platform}",
        style="dim",
    )
    return Panel(Group(line, sub), border_style="blue", padding=(0, 1))


def _recommendation_body(advice: Advice):
    """Build the headline panel's lines once, so its height can be derived.

    The live layout pins this panel to a fixed number of rows; deriving that
    number from the actual content stops a line from being silently clipped
    when the panel grows.
    """
    if advice.state.is_complete:
        done = Text()
        done.append("Your team is set — ", style="dim")
        done.append(f"{advice.projected_starters:.0f} projected points", style="bold white")
        done.append(" from starters", style="dim")
        return [Align.center(done)], None

    if not advice.recommendations:
        return [Align.center(Text("No players available.", style="dim"))], None

    top = advice.realistic or advice.recommendations[0]
    p = top.player

    title = Text()
    title.append(p.name, style="bold white")
    title.append(f"  {p.position}", style=f"bold {POS_STYLE.get(p.position, 'white')}")
    title.append(f" · {p.team or 'FA'}", style="white")
    if p.bye_week:
        title.append(f" · bye {p.bye_week}", style="dim")

    stats = Text()
    stats.append(f"proj {p.proj_points:.0f}", style="white")
    stats.append("   VOR ", style="dim")
    stats.append(f"{p.vor:+.1f}", style="bold green" if p.vor > 0 else "red")
    stats.append("   VONA ", style="dim")
    stats.append(f"{top.vona:+.1f}", style="bold cyan")
    stats.append(f"   {p.position}{p.pos_rank} · tier {p.tier}", style="dim")
    if advice.is_my_turn:
        stats.append("   lasts to your next ", style="dim")
        stats.append_text(_survival_text(top.survival))
    else:
        stats.append("   reaches your pick ", style="dim")
        stats.append_text(_survival_text(top.hold))

    body = [Align.center(title), Align.center(stats)]
    if top.reasons:
        body.append(Align.center(Text(" · ".join(top.reasons), style="italic yellow")))

    # Name the higher-priority players to grab instead if the board breaks well.
    if not advice.is_my_turn:
        fallers = [r for r in advice.recommendations if r.score > top.score][:3]
        if fallers:
            line = Text("take instead if he falls: ", style="dim")
            for i, r in enumerate(fallers):
                if i:
                    line.append(" · ", style="dim")
                line.append(r.player.name, style="bold white")
                line.append(f" {r.hold:.0%}", style="dim")
            body.append(Align.center(line))
    return body, top


def recommendation_height(advice: Advice) -> int:
    """Rows the headline panel needs: content + borders + vertical padding."""
    body, _ = _recommendation_body(advice)
    return len(body) + 4


def recommendation(advice: Advice) -> Panel:
    body, top = _recommendation_body(advice)
    if top is None:
        title = ("[bold]DRAFT COMPLETE[/bold]" if advice.state.is_complete
                 else "[bold]PICK[/bold]")
        return Panel(Group(*body), title=title, border_style="blue", padding=(1, 1))

    mine = advice.is_my_turn
    # this_pick is None once you have no picks left.
    title = (
        "[bold]★ YOUR PICK[/bold]" if mine
        else f"[bold]★ PLAN FOR PICK #{advice.this_pick}[/bold]" if advice.this_pick
        else "[bold]★ BEST AVAILABLE[/bold]"
    )
    return Panel(
        Group(*body),
        title=title,
        border_style="bright_yellow" if mine else "blue",
        padding=(1, 1),
    )


def targets(advice: Advice, n: int = 12) -> Panel:
    t = Table(expand=True, box=None, pad_edge=False, show_edge=False)
    t.add_column("#", width=2, style="dim")
    t.add_column("Player", ratio=3, no_wrap=True)
    t.add_column("Pos", width=4)
    t.add_column("Tm", width=4, style="dim")
    t.add_column("Bye", width=3, style="dim", justify="right")
    t.add_column("Proj", width=5, justify="right")
    t.add_column("VOR", width=6, justify="right")
    t.add_column("VONA", width=6, justify="right")
    t.add_column("Lasts" if advice.is_my_turn else "Reach", width=5, justify="right")
    t.add_column("Note", ratio=2, no_wrap=True, style="italic dim")

    for i, r in enumerate(advice.recommendations[:n], 1):
        p = r.player
        style = "bold" if i == 1 else ""
        t.add_row(
            str(i),
            Text(p.name, style=style),
            _pos(p),
            p.team or "FA",
            str(p.bye_week or "-"),
            f"{p.proj_points:.0f}",
            Text(f"{p.vor:+.1f}", style="green" if p.vor > 0 else "red"),
            Text(f"{r.vona:+.1f}", style="cyan"),
            _survival_text(r.survival if advice.is_my_turn else r.hold),
            "; ".join(r.reasons)[:40],
        )
    return Panel(t, title="[bold]TOP TARGETS[/bold]", border_style="blue", padding=(0, 1))


# Order bench players are listed in, so depth at a position reads at a glance.
_BENCH_ORDER = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "K": 4, "DEF": 5}


def roster(advice: Advice) -> Panel:
    t = Table(expand=True, box=None, show_header=False, pad_edge=False)
    t.add_column("Slot", width=6, style="dim")
    t.add_column("Player", ratio=2, no_wrap=True)
    t.add_column("Pos", width=4)
    t.add_column("Proj", width=5, justify="right", style="dim")

    starters = set()
    for slot, pl in advice.lineup.assignment:
        if pl:
            starters.add(pl.player_id)
            t.add_row(
                slot,
                Text(pl.name, style=POS_STYLE.get(pl.position, "white")),
                _pos(pl),
                f"{pl.proj_points:.0f}",
            )
        else:
            t.add_row(slot, Text("— open —", style="red dim"), "", "")

    # Bench: a FLEX row does not reveal a position either, so both get one.
    bench = sorted(
        (p for p in advice.my_players if p.player_id not in starters),
        key=lambda p: (_BENCH_ORDER.get(p.position, 9), -p.proj_points),
    )
    for pl in bench:
        t.add_row(
            Text("BN", style="dim"),
            Text(pl.name, style="dim"),
            _pos(pl),
            f"{pl.proj_points:.0f}",
        )

    footer = Text()
    footer.append(f"projected starters: {advice.projected_starters:.0f} pts", style="bold white")
    byes: dict[int, int] = {}
    for _, pl in advice.lineup.assignment:
        if pl and pl.bye_week:
            byes[pl.bye_week] = byes.get(pl.bye_week, 0) + 1
    stacked = [f"wk{w}×{c}" for w, c in sorted(byes.items()) if c >= 2]
    if stacked:
        footer.append("   bye stacks: " + ", ".join(stacked), style="yellow")

    return Panel(Group(t, footer), title="[bold]YOUR ROSTER[/bold]",
                 border_style="blue", padding=(0, 1))


def outlook(advice: Advice) -> Panel:
    t = Table(expand=True, box=None, pad_edge=False)
    t.add_column("Pos", width=4)
    t.add_column("At pick", width=7, justify="right")
    t.add_column("Next", width=6, justify="right")
    t.add_column("Drop", width=7, justify="right")
    t.add_column("Need", width=4, justify="right", style="dim")
    for o in advice.outlook:
        drop_style = "bold red" if o.dropoff > 20 else "yellow" if o.dropoff > 8 else "dim"
        t.add_row(
            Text(o.position, style=POS_STYLE.get(o.position, "white")),
            f"{o.best_now:.0f}",
            f"{o.expected_next:.0f}",
            Text(f"{o.dropoff:+.0f}", style=drop_style),
            str(o.starters_needed),
        )
    note = Text("drop = value lost by waiting one more turn at this position",
                style="dim italic")
    return Panel(Group(t, note), title="[bold]POSITION SCARCITY[/bold]",
                 border_style="blue", padding=(0, 1))


def gone_soon(advice: Advice) -> Panel:
    t = Table(expand=True, box=None, show_header=False, pad_edge=False)
    t.add_column("Player", ratio=2, no_wrap=True)
    t.add_column("Pos", width=4)
    t.add_column("Held", width=4, justify="right")
    if not advice.likely_gone:
        t.add_row(Text("nothing urgent", style="dim"), "", "")
    for p, s in advice.likely_gone:
        t.add_row(Text(p.name), _pos(p), _survival_text(s))
    title = ("GONE BY YOUR NEXT PICK" if advice.is_my_turn or not advice.this_pick
             else f"GONE BEFORE PICK #{advice.this_pick}")
    return Panel(t, title=f"[bold]{title}[/bold]", border_style="blue", padding=(0, 1))


def recent(advice: Advice, pool: dict[str, Player], n: int = 10) -> Panel:
    st = advice.state
    if not st.picks:
        return Panel(
            Align.center(Text("draft has not started", style="dim")),
            title="[bold]RECENT PICKS[/bold]", border_style="blue", padding=(0, 1),
        )

    t = Table(expand=True, box=None, show_header=False, pad_edge=False)
    t.add_column("Pk", width=6, style="dim")
    t.add_column("Team", ratio=1, no_wrap=True, style="dim")
    t.add_column("Player", ratio=2, no_wrap=True)
    t.add_column("Pos", width=4)

    for pick in st.picks[-n:][::-1]:
        p = pool.get(pick.player_id or "")
        rnd, pk = st.round_and_pick(pick.pick_no)
        who = st.slot_names.get(pick.slot, f"T{pick.slot}")
        mine = pick.slot == st.my_slot
        t.add_row(
            f"{rnd}.{pk:02d}",
            Text(who, style="bold yellow" if mine else "dim"),
            Text(p.name if p else "—", style="white"),
            _pos(p) if p else Text("", style="dim"),
        )
    return Panel(t, title="[bold]RECENT PICKS[/bold]", border_style="blue", padding=(0, 1))


def render(advice: Advice, pool: dict[str, Player], status: str = "") -> Layout:
    root = Layout()
    root.split_column(
        Layout(header(advice), name="header", size=4),
        Layout(recommendation(advice), name="rec", size=recommendation_height(advice)),
        Layout(name="mid", ratio=3),
        Layout(name="bottom", ratio=2),
        Layout(Text(status, style="dim"), name="status", size=1),
    )
    root["mid"].split_row(
        Layout(targets(advice), name="targets", ratio=3),
        Layout(roster(advice), name="roster", ratio=2),
    )
    root["bottom"].split_row(
        Layout(outlook(advice), ratio=2),
        Layout(gone_soon(advice), ratio=2),
        Layout(recent(advice, pool), ratio=2),
    )
    return root


def print_static(advice: Advice, pool: dict[str, Player], console: Console) -> None:
    """One-shot render, for `ffm pick` and for terminals without a live area."""
    console.print(header(advice))
    console.print(recommendation(advice))
    console.print(targets(advice))
    console.print(roster(advice))
    console.print(outlook(advice))
