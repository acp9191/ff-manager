"""ffm — fantasy football draft manager."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from ffm import cache
from ffm.platforms.base import AdapterError

console = Console()


def _load_dotenv() -> None:
    for path in (Path.cwd() / ".env", Path.home() / ".config" / "ff-manager" / ".env"):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


# ---- commands ----------------------------------------------------------

def cmd_sleeper_drafts(args) -> int:
    from ffm.data.sleeper_data import current_season
    from ffm.platforms.sleeper import list_drafts

    season = args.season or current_season()
    drafts = list_drafts(args.user, season)
    if not drafts:
        console.print(f"[yellow]No {season} drafts found for '{args.user}'.[/yellow]")
        return 1

    t = Table(title=f"Sleeper drafts for {args.user} ({season})")
    t.add_column("draft_id", style="cyan", no_wrap=True, overflow="fold")
    t.add_column("League", max_width=22)
    t.add_column("Status")
    t.add_column("Teams", justify="right")
    t.add_column("Rounds", justify="right")
    t.add_column("Type")
    for d in drafts:
        s = d.get("settings") or {}
        t.add_row(
            d.get("draft_id", ""),
            (d.get("metadata") or {}).get("name") or "(mock)",
            d.get("status", ""),
            str(s.get("teams", "")),
            str(s.get("rounds", "")),
            d.get("type", ""),
        )
    console.print(t)
    console.print("\nStart the board with: [bold]ffm run --sleeper <draft_id> --user "
                  f"{args.user}[/bold]")
    return 0


def cmd_yahoo_auth(args) -> int:
    from ffm.platforms.yahoo import YahooAuth

    cid = os.environ.get("YAHOO_CLIENT_ID")
    secret = os.environ.get("YAHOO_CLIENT_SECRET")
    if not cid or not secret:
        console.print(
            "[red]Missing YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET.[/red]\n"
            "Create an app at https://developer.yahoo.com/apps/create/ "
            "(API Permissions: Fantasy Sports, Read), then put the keys in .env"
        )
        return 1
    auth = YahooAuth(cid, secret, redirect_uri=args.redirect)

    problem = auth.preflight()
    if problem:
        console.print(f"[red]{problem}[/red]")
        return 1

    # Exchanging a code the user already has needs no terminal interaction.
    if args.code:
        auth.exchange(args.code)
        console.print("[green]Yahoo authorized.[/green] Now run: "
                      "[bold]ffm yahoo leagues[/bold]")
        return 0

    # Only prompt when there is a real terminal to prompt on; otherwise print
    # the URL and let the user come back with the code.
    if sys.stdin.isatty() and not args.print_url:
        auth.interactive_login(console)
        console.print("[green]Yahoo authorized.[/green] Now run: "
                      "[bold]ffm yahoo leagues[/bold]")
        return 0

    console.print("Open this URL and approve access:\n")
    # Plain stdout: rich would wrap the URL and make it uncopyable.
    print(auth.authorize_url(), flush=True)
    console.print("\nThen run: [bold]ffm yahoo-auth --code <the code Yahoo shows>[/bold]")
    return 0


def cmd_yahoo_leagues(args) -> int:
    from ffm.platforms.yahoo import YahooAuth, list_leagues

    auth = YahooAuth(os.environ.get("YAHOO_CLIENT_ID", ""),
                     os.environ.get("YAHOO_CLIENT_SECRET", ""))
    leagues = list_leagues(auth)
    if not leagues:
        console.print("[yellow]No Yahoo NFL leagues found.[/yellow]")
        return 1
    t = Table(title="Yahoo leagues")
    t.add_column("league_key", style="cyan")
    t.add_column("Name")
    t.add_column("Teams", justify="right")
    t.add_column("Season")
    t.add_column("Draft")
    for lg in leagues:
        t.add_row(lg["league_key"], str(lg.get("name")), str(lg.get("num_teams") or ""),
                  str(lg.get("season") or ""), str(lg.get("draft_status") or ""))
    console.print(t)
    console.print("\nStart the board with: [bold]ffm run --yahoo <league_key>[/bold]")
    return 0


def _build_adapter(args):
    if args.sleeper:
        from ffm.platforms.sleeper import SleeperAdapter
        return SleeperAdapter(args.sleeper, username=args.user, my_slot=args.slot)
    if args.yahoo:
        from ffm.platforms.yahoo import YahooAdapter, YahooAuth
        auth = YahooAuth(os.environ.get("YAHOO_CLIENT_ID", ""),
                         os.environ.get("YAHOO_CLIENT_SECRET", ""))
        return YahooAdapter(args.yahoo, auth, my_slot=args.slot)
    raise AdapterError("Pass --sleeper <draft_id> or --yahoo <league_key>.")


def cmd_run(args) -> int:
    from ffm.runner import build_session, live_loop

    adapter = _build_adapter(args)
    with console.status("[cyan]Loading league settings and projections…"):
        session = build_session(adapter, bpa_weight=args.bpa)
    s = adapter.league_settings()
    console.print(
        f"[green]Loaded[/green] {s.name} — {s.teams} teams, {s.rounds} rounds, "
        f"{s.scoring_type}, {len(session.pool)} players projected."
    )
    live_loop(session, console, interval=args.interval, bell=not args.no_bell)
    return 0


def cmd_pick(args) -> int:
    """One-shot: print the recommendation and exit."""
    from ffm.runner import build_session
    from ffm.ui import dashboard

    adapter = _build_adapter(args)
    with console.status("[cyan]Loading…"):
        session = build_session(adapter, bpa_weight=args.bpa)
        advice = session.advice()
    dashboard.print_static(advice, session.pool, console)
    return 0


def cmd_sim(args) -> int:
    from ffm.data.sleeper_data import build_player_pool
    from ffm.platforms.mock import MockAdapter
    from ffm.runner import Session, live_loop

    mock = MockAdapter(
        build_player_pool,
        teams=args.teams,
        rounds=args.rounds,
        my_slot=args.slot or 5,
        scoring_type=args.scoring,
        seed=args.seed,
    )
    with console.status("[cyan]Building player pool…"):
        pool = mock.pool()

    if args.auto:
        # Fully automatic rehearsal: the engine drafts for you every turn.
        from ffm.engine.recommend import analyze
        from ffm.ui import dashboard

        while True:
            mock.simulate_until_my_turn()
            state = mock.fetch_state()
            if state.is_complete:
                break
            advice = analyze(state, pool, bpa_weight=args.bpa)
            top = advice.recommendations[0]
            r, p = state.round_and_pick(state.current_pick_no)
            console.print(
                f"[dim]R{r}.{p:02d}[/dim] → [bold]{top.player.name}[/bold] "
                f"[cyan]{top.player.position}[/cyan] "
                f"(vona {top.vona:+.1f}) {'; '.join(top.reasons)}"
            )
            mock.draft_player(top.player.player_id)
        final = analyze(mock.fetch_state(), pool, bpa_weight=args.bpa)
        dashboard.print_static(final, pool, console)
        return 0

    session = Session(adapter=mock, pool=pool, bpa_weight=args.bpa)
    console.print("[green]Mock draft ready.[/green] Bots pick automatically between your turns.")

    def step(sess):
        """Advance the simulation: bots pick, then the engine picks for you."""
        mock.simulate_until_my_turn()
        state = mock.fetch_state()
        if state.is_my_turn() and not state.is_complete:
            mock.draft_player(sess.advice().recommendations[0].player.player_id)

    live_loop(session, console, interval=args.interval, bell=False, step_hook=step)
    return 0


def cmd_check(args) -> int:
    """Run the engine across simulated drafts and assert it stays coherent."""
    from ffm.data.sleeper_data import build_player_pool
    from ffm.engine.audit import check_advice
    from ffm.engine.recommend import analyze
    from ffm.platforms.mock import MockAdapter

    SF = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "SUPER_FLEX", "K", "DEF"] + ["BN"] * 6
    configs = [
        dict(teams=12, rounds=15, my_slot=9, scoring_type="half_ppr"),
        dict(teams=10, rounds=16, my_slot=1, scoring_type="ppr"),
        dict(teams=14, rounds=15, my_slot=14, scoring_type="std"),
        dict(teams=12, rounds=16, my_slot=6, scoring_type="half_ppr", roster_slots=SF),
    ]
    failures: list[str] = []
    states = 0
    with console.status("[cyan]Auditing engine across simulated drafts…"):
        for i, cfg in enumerate(configs):
            mock = MockAdapter(build_player_pool, seed=20 + i, **cfg)
            pool = mock.pool()
            while True:
                state = mock.fetch_state()
                if state.is_complete:
                    break
                advice = analyze(state, pool, bpa_weight=args.bpa)
                states += 1
                failures.extend(f"[{cfg['teams']}tm {cfg['scoring_type']}] {m}"
                                for m in check_advice(advice))
                mock.simulate_until_my_turn()
                s2 = mock.fetch_state()
                if s2.is_complete:
                    break
                mock.draft_player(analyze(s2, pool).recommendations[0].player.player_id)

    if failures:
        console.print(f"[red]{len(failures)} inconsistencies across {states} states:[/red]")
        for f in dict.fromkeys(failures[:20]):
            console.print(f"  [red]·[/red] {f}")
        return 1
    console.print(f"[green]OK[/green] — {states} draft states audited, no inconsistencies.")
    return 0


def cmd_leagues(args) -> int:
    from ffm.data.sleeper_data import current_season
    from ffm.platforms.league import list_leagues

    season = args.season or current_season()
    leagues = list_leagues(args.user, season)
    if not leagues:
        console.print(f"[yellow]No {season} leagues found for '{args.user}'.[/yellow]")
        return 1

    t = Table(title=f"Sleeper leagues for {args.user} ({season})")
    t.add_column("league_id", style="cyan", no_wrap=True, overflow="fold")
    t.add_column("Name", max_width=28)
    t.add_column("Teams", justify="right")
    t.add_column("Status")
    for lg in leagues:
        t.add_row(str(lg.get("league_id", "")), str(lg.get("name", "")),
                  str(lg.get("total_rosters", "")), str(lg.get("status", "")))
    console.print(t)
    console.print("\nStart with: [bold]ffm week --league <league_id> --user "
                  f"{args.user}[/bold]")
    return 0


def _season_session(args):
    from ffm.season import build_season

    if not args.league:
        raise AdapterError(
            "Pass --league <league_id>. Run `ffm leagues --user <name>` to find it."
        )
    return build_season(
        args.league,
        username=args.user,
        roster_id=args.roster_id,
        week=args.week,
        lookback=args.lookback,
        horizon_weeks=getattr(args, "horizon", 4),
        opinions_path=Path(args.opinions) if args.opinions else None,
    )


def cmd_week(args) -> int:
    """The weekly report: what is broken, who to start, and who to add."""
    from ffm.ui import week as week_ui

    from ffm import brief as brief_mod

    with console.status("[cyan]Loading league, projections and context…"):
        sess = _season_session(args)
        plan = sess.plan()
        board = None if args.no_waivers else sess.board(limit=6)

    written = None
    if args.brief:
        if not brief_mod.configured():
            console.print("[yellow]--brief needs OPENROUTER_API_KEY in .env "
                          "(see `ffm models` for model ids).[/yellow]")
        else:
            with console.status("[magenta]Writing the summary…"):
                written = brief_mod.for_report(plan, board, model=args.model)
            if written is None:
                console.print("[dim]Summary unavailable — showing the "
                              "computed reasons only.[/dim]")
    week_ui.print_week(plan, sess.settings.name, console, board=board,
                       show_bench=not args.no_bench, brief=written)
    _opinions_hint(sess)
    return 0


def cmd_waivers(args) -> int:
    from ffm.ui import week as week_ui

    with console.status("[cyan]Loading league, projections and context…"):
        sess = _season_session(args)
        board = sess.board(limit=args.limit)
    week_ui.print_waivers(board, sess.settings.name, console, limit=args.limit)
    _opinions_hint(sess)
    return 0


def _opinions_hint(sess) -> None:
    src = sess.ctx.opinions.source
    if src:
        console.print(f"[dim]Your reads applied from {src}[/dim]")
    else:
        console.print(
            "[dim]No opinions.toml found — copy opinions.example.toml to "
            "./opinions.toml to fade teams you do not trust.[/dim]"
        )


def cmd_telegram_setup(args) -> int:
    """Find your chat id, which is the one fiddly part of Telegram setup."""
    import requests

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        console.print(
            "[red]No TELEGRAM_BOT_TOKEN set.[/red]\n\n"
            "1. Open Telegram and message [bold]@BotFather[/bold]\n"
            "2. Send [bold]/newbot[/bold] and follow the two prompts\n"
            "3. It replies with a token like [dim]8123456:AAH...[/dim]\n"
            "4. Put it in .env as [bold]TELEGRAM_BOT_TOKEN=...[/bold]\n"
            "5. Send your new bot any message (say 'hi'), then run this again"
        )
        return 1

    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
    if r.status_code == 401:
        console.print("[red]That token was rejected by Telegram.[/red]")
        return 1
    r.raise_for_status()
    chats = {}
    for u in (r.json().get("result") or []):
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or "you"

    if not chats:
        console.print(
            "[yellow]The bot has not received any messages yet.[/yellow]\n"
            "Open Telegram, find your bot, send it any message, then run this again.\n"
            "[dim](Telegram only reveals a chat id once that chat has messaged the "
            "bot — bots cannot start conversations.)[/dim]"
        )
        return 1

    t = Table(title="Chats your bot can reach")
    t.add_column("chat_id", style="cyan")
    t.add_column("Who")
    for cid, who in chats.items():
        t.add_row(str(cid), str(who))
    console.print(t)
    first = next(iter(chats))
    console.print(f"\nAdd to your .env:\n[bold]TELEGRAM_CHAT_ID={first}[/bold]")
    console.print("\nThen check it works: [bold]ffm notify-test[/bold]")
    return 0


def cmd_notify_test(args) -> int:
    from ffm.notify import Message, from_env

    n = from_env(console)
    if n.name == "console":
        console.print(
            "[yellow]No notifier configured — falling back to the console.[/yellow]\n"
            "For Telegram, run [bold]ffm telegram-setup[/bold]."
        )
    n.send(Message(
        title="ff-manager is wired up",
        lines=["If you can read this on your phone, alerts will reach you.",
               "Start the watcher with: ffm watch --league <id> --user <name>"],
    ))
    console.print(f"[green]Sent via {n.describe()}.[/green]")
    return 0


def cmd_watch(args) -> int:
    from pathlib import Path as _Path

    from ffm.notify import from_env
    from ffm.watch import parse_digest_times, watch

    if not args.league:
        raise AdapterError(
            "Pass --league <league_id>. Run `ffm leagues --user <name>` to find it."
        )
    notifier = from_env(console)
    if notifier.name == "console" and not args.allow_console:
        console.print(
            "[red]No notifier configured, so nothing would reach your phone.[/red]\n"
            "Run [bold]ffm telegram-setup[/bold], or pass --allow-console to print "
            "to this terminal instead."
        )
        return 1
    try:
        digest_times = parse_digest_times(args.digest_at)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return 2

    watch(
        args.league,
        notifier,
        console,
        interval=args.interval,
        once=args.once,
        freshness=args.freshness,
        username=args.user,
        roster_id=args.roster_id,
        week=args.week,
        horizon=args.horizon,
        lookback=args.lookback,
        opinions_path=_Path(args.opinions) if args.opinions else None,
        digest_times=digest_times,
        skip_waivers=args.no_waivers,
        want_brief=args.brief,
        speculative=args.speculative,
    )
    return 0


def cmd_version(args) -> int:
    """Version and content hash. Compare with the container to spot a stale image."""
    from pathlib import Path

    import ffm
    console.print(f"ff-manager {ffm.__version__}")
    console.print(f"build   {ffm.build_id()}")
    console.print(f"loaded  {Path(ffm.__file__).resolve().parent}")
    return 0


def cmd_health(args) -> int:
    """Exit 0 if the watcher completed a pass recently. For healthchecks."""
    from ffm.watch import last_beat

    age = last_beat()
    if age is None:
        console.print("[yellow]no pass recorded yet[/yellow]")
        return 1
    if age > args.max_age:
        console.print(f"[red]last successful pass was {age / 60:.0f} min ago "
                      f"(limit {args.max_age / 60:.0f} min)[/red]")
        return 1
    console.print(f"[green]ok[/green] — last pass {age / 60:.0f} min ago")
    return 0


def cmd_news(args) -> int:
    """Injuries, depth-chart promotions, and market moves, right now."""
    from ffm.data import weekly
    from ffm.engine import news
    from ffm.ui import week as week_ui

    with console.status("[cyan]Scanning for injuries and depth-chart changes…"):
        weekly.set_freshness(args.freshness)
        sess = _season_session(args)
        market = weekly.trending("add", hours=48, limit=120)
        # A one-shot scan has no previous poll to diff against, so an empty
        # snapshot is correct: it reports everything standing right now.
        items, _ = news.scan(sess.state, sess.pool, market, news.Snapshot(),
                             sess.advisor())
    week_ui.print_news(items, sess.settings.name, sess.week, console)
    return 0


def cmd_models(args) -> int:
    """List OpenRouter model ids, so none of them has to be guessed."""
    from ffm.brief import DEFAULT_MODEL, list_models

    rows = list_models(args.filter or "")
    if not rows:
        console.print(f"[yellow]No models matching '{args.filter}'.[/yellow]")
        return 1
    t = Table(title=f"OpenRouter models{' matching ' + args.filter if args.filter else ''}")
    t.add_column("id", style="cyan", no_wrap=True)
    t.add_column("$/1M in", justify="right")
    t.add_column("$/1M out", justify="right")
    t.add_column("Context", justify="right")
    for m in rows[: args.limit]:
        pr = m.get("pricing") or {}
        t.add_row(
            m.get("id", ""),
            f"{float(pr.get('prompt') or 0) * 1e6:.2f}",
            f"{float(pr.get('completion') or 0) * 1e6:.2f}",
            str(m.get("context_length") or ""),
        )
    console.print(t)
    console.print(f"\nDefault is [bold]{DEFAULT_MODEL}[/bold]. Override with "
                  "OPENROUTER_MODEL in .env, or --model.")
    return 0


def cmd_cache_clear(args) -> int:
    console.print(f"Cleared {cache.clear()} cached files from {cache.CACHE_DIR}")
    return 0


# ---- parser ------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ffm", description="Fantasy football manager: draft, start/sit and waivers")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        sp.add_argument("--sleeper", metavar="DRAFT_ID", help="Sleeper draft id")
        sp.add_argument("--yahoo", metavar="LEAGUE_KEY", help="Yahoo league key")
        sp.add_argument("--user", help="Sleeper username (to find your draft slot)")
        sp.add_argument("--slot", type=int, help="Your draft slot, 1 = first pick")
        sp.add_argument("--bpa", type=float, default=0.20,
                        help="0 = pure scarcity, higher = best-player-available (default 0.20)")

    sd = sub.add_parser("drafts", help="List your Sleeper drafts")
    sd.add_argument("--user", required=True)
    sd.add_argument("--season")
    sd.set_defaults(func=cmd_sleeper_drafts)

    ya = sub.add_parser("yahoo-auth", help="Authorize with Yahoo")
    ya.add_argument("--redirect", default="oob")
    ya.add_argument("--code", help="Verification code from Yahoo (skips the prompt)")
    ya.add_argument("--print-url", action="store_true",
                    help="Just print the authorize URL and exit")
    ya.set_defaults(func=cmd_yahoo_auth)

    yl = sub.add_parser("yahoo", help="Yahoo helpers")
    ysub = yl.add_subparsers(dest="ycmd", required=True)
    yll = ysub.add_parser("leagues", help="List your Yahoo leagues")
    yll.set_defaults(func=cmd_yahoo_leagues)

    r = sub.add_parser("run", help="Live draft dashboard")
    add_common(r)
    r.add_argument("--interval", type=float, default=5.0, help="Poll seconds (default 5)")
    r.add_argument("--no-bell", action="store_true", help="Do not beep on your turn")
    r.set_defaults(func=cmd_run)

    pk = sub.add_parser("pick", help="Print the current recommendation once")
    add_common(pk)
    pk.set_defaults(func=cmd_pick)

    sm = sub.add_parser("sim", help="Offline mock draft to rehearse")
    sm.add_argument("--teams", type=int, default=12)
    sm.add_argument("--rounds", type=int, default=15)
    sm.add_argument("--slot", type=int, default=5)
    sm.add_argument("--scoring", default="half_ppr", choices=["ppr", "half_ppr", "std"])
    sm.add_argument("--seed", type=int, default=0)
    sm.add_argument("--interval", type=float, default=2.0)
    sm.add_argument("--bpa", type=float, default=0.20)
    sm.add_argument("--auto", action="store_true", help="Run the whole draft and print the result")
    sm.set_defaults(func=cmd_sim)

    ck = sub.add_parser("check", help="Audit the engine for internal inconsistencies")
    ck.add_argument("--bpa", type=float, default=0.20)
    ck.set_defaults(func=cmd_check)

    def add_league(sp):
        sp.add_argument("--league", metavar="LEAGUE_ID", help="Sleeper league id")
        sp.add_argument("--user", help="Your Sleeper username (to find your team)")
        sp.add_argument("--roster-id", type=int, dest="roster_id",
                        help="Your roster id, if the username lookup cannot be used")
        sp.add_argument("--week", type=int, help="Week to analyze (default: current)")
        sp.add_argument("--lookback", type=int, default=5,
                        help="Games of history behind the context signals (default 5)")
        sp.add_argument("--opinions", help="Path to an opinions.toml")
        sp.add_argument("--brief", action="store_true",
                        help="Add a written summary via OpenRouter (needs a key)")
        sp.add_argument("--model", help="OpenRouter model id (see `ffm models`)")

    lg = sub.add_parser("leagues", help="List your Sleeper leagues")
    lg.add_argument("--user", required=True)
    lg.add_argument("--season")
    lg.set_defaults(func=cmd_leagues)

    wk = sub.add_parser("week", help="Weekly report: alerts, start/sit and waivers")
    add_league(wk)
    wk.add_argument("--horizon", type=int, default=4,
                    help="Weeks ahead used to value waiver adds (default 4)")
    wk.add_argument("--no-waivers", action="store_true", help="Skip the waiver section")
    wk.add_argument("--no-bench", action="store_true", help="Skip the bench table")
    wk.set_defaults(func=cmd_week)

    wv = sub.add_parser("waivers", help="Waiver-wire targets and who to drop")
    add_league(wv)
    wv.add_argument("--horizon", type=int, default=4,
                    help="Weeks ahead used to value adds (default 4)")
    wv.add_argument("--limit", type=int, default=12)
    wv.set_defaults(func=cmd_waivers)

    wt = sub.add_parser("watch", help="Run continuously and push alerts to your phone")
    add_league(wt)
    wt.add_argument("--horizon", type=int, default=4)
    wt.add_argument("--interval", type=float, default=1800.0,
                    help="Seconds between checks (default 1800 = 30 min)")
    wt.add_argument("--digest-at", default="09:00", dest="digest_at",
                    help="Times to send a full digest, e.g. '09:00,17:30'. "
                         "Empty string for alerts only.")
    wt.add_argument("--freshness", type=int,
                    help="Seconds before live data is refetched "
                         "(default: half the poll interval)")
    wt.add_argument("--once", action="store_true",
                    help="Check once and exit — the mode to use from cron/launchd")
    wt.add_argument("--no-waivers", action="store_true")
    wt.add_argument("--alert-speculative", action="store_true",
                    dest="speculative",
                    help="Also push flyers and market spikes. Off by default: "
                         "they still appear on the waiver board.")
    wt.add_argument("--allow-console", action="store_true",
                    help="Print to the terminal when no notifier is configured")
    wt.set_defaults(func=cmd_watch)

    ts = sub.add_parser("telegram-setup", help="Find your Telegram chat id")
    ts.set_defaults(func=cmd_telegram_setup)

    nt = sub.add_parser("notify-test", help="Send yourself a test notification")
    nt.set_defaults(func=cmd_notify_test)

    vr = sub.add_parser("version", help="Version and build hash of this install")
    vr.set_defaults(func=cmd_version)

    hl = sub.add_parser("health", help="Exit non-zero if the watcher has stalled")
    hl.add_argument("--max-age", type=float, default=5400, dest="max_age",
                    help="Seconds since the last successful pass (default 5400)")
    hl.set_defaults(func=cmd_health)

    nw = sub.add_parser("news", help="Injuries, promotions and market moves now")
    add_league(nw)
    nw.add_argument("--horizon", type=int, default=4)
    nw.add_argument("--freshness", type=int, default=600,
                    help="Seconds before live data is refetched (default 600)")
    nw.set_defaults(func=cmd_news)

    md = sub.add_parser("models", help="List OpenRouter models for --brief")
    md.add_argument("--filter", help="Substring to match, e.g. 'anthropic'")
    md.add_argument("--limit", type=int, default=30)
    md.set_defaults(func=cmd_models)

    cc = sub.add_parser("cache-clear", help="Drop cached player/projection data")
    cc.set_defaults(func=cmd_cache_clear)
    return p


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except AdapterError as e:
        console.print(f"[red]{e}[/red]")
        return 2
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/dim]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
