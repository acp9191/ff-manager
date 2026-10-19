"""Getting the advice to your phone before kickoff.

These are deadline alerts. "Your RB2 is Out" at 11am is worth something and at
2pm is worth nothing, so the only delivery channels here are ones that produce
a real push notification on a phone. Email and desktop notifications are
deliberately absent: the first gets buried, the second only works when you are
sitting at the machine, which is not where you are on a Sunday morning.

The backends differ only in a URL and a payload shape, so all of them are
supported rather than picking one for you. Configure whichever you like in
`.env`; the first one configured wins unless FFM_NOTIFY names one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import requests

TIMEOUT = 15


@dataclass
class Message:
    """One notification. `key` is what stops it being sent twice."""

    title: str
    lines: list[str] = field(default_factory=list)
    urgent: bool = False
    key: str = ""

    def plain(self) -> str:
        return "\n".join([self.title, *self.lines]).strip()


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Notifier:
    name = "none"

    def send(self, msg: Message) -> bool:
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


class ConsoleNotifier(Notifier):
    """Prints instead of sending. The default when nothing is configured."""

    name = "console"

    def __init__(self, console=None):
        self._console = console

    def send(self, msg: Message) -> bool:
        text = msg.plain()
        if self._console:
            style = "bold red" if msg.urgent else "cyan"
            self._console.print(f"[{style}]{msg.title}[/{style}]")
            for line in msg.lines:
                self._console.print(f"  {line}")
        else:
            print(text)
        return True


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    def send(self, msg: Message) -> bool:
        body = "\n".join(
            [f"<b>{_esc(msg.title)}</b>", *(_esc(l) for l in msg.lines)]
        )
        r = requests.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": body[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return True

    def describe(self) -> str:
        return f"telegram (chat {self.chat_id})"


class NtfyNotifier(Notifier):
    """ntfy.sh — no account and no token, just a topic you subscribe to."""

    name = "ntfy"

    def __init__(self, topic: str, server: str = "https://ntfy.sh"):
        self.topic = topic
        self.server = server.rstrip("/")

    def send(self, msg: Message) -> bool:
        r = requests.post(
            f"{self.server}/{self.topic}",
            data="\n".join(msg.lines).encode("utf-8")[:3800],
            headers={
                "Title": msg.title.encode("ascii", "ignore").decode(),
                "Priority": "high" if msg.urgent else "default",
                "Tags": "rotating_light" if msg.urgent else "football",
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return True

    def describe(self) -> str:
        return f"ntfy ({self.server}/{self.topic})"


class DiscordNotifier(Notifier):
    name = "discord"

    def __init__(self, webhook: str):
        self.webhook = webhook

    def send(self, msg: Message) -> bool:
        body = "\n".join([f"**{msg.title}**", *msg.lines])
        r = requests.post(
            self.webhook, json={"content": body[:1900]}, timeout=TIMEOUT
        )
        r.raise_for_status()
        return True


def from_env(console=None) -> Notifier:
    """Build whichever notifier the environment is configured for."""
    choice = (os.environ.get("FFM_NOTIFY") or "").strip().lower()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    topic = os.environ.get("NTFY_TOPIC")
    hook = os.environ.get("DISCORD_WEBHOOK_URL")

    if choice == "console":
        return ConsoleNotifier(console)
    if choice in ("", "telegram") and token and chat:
        return TelegramNotifier(token, chat)
    if choice in ("", "ntfy") and topic:
        return NtfyNotifier(topic, os.environ.get("NTFY_SERVER", "https://ntfy.sh"))
    if choice in ("", "discord") and hook:
        return DiscordNotifier(hook)
    return ConsoleNotifier(console)


# ---- turning reports into messages -------------------------------------

def _who(p) -> str:
    where = f" {'vs' if p.home else '@'} {p.opponent}" if p.opponent else ", BYE"
    return f"{p.name} ({p.position}-{p.team or 'FA'}{where})"


def _move_lines(move) -> list[str]:
    """The verdict, then the reasoning. Same shape wherever a pickup appears."""
    if move is None:
        return []
    return [f"-> {move.summary}"] + [f"   {l}" for l in move.lines]


def alert_messages(plan) -> list[Message]:
    """Urgent lineup problems, each with the fix attached.

    An alert that only says something is wrong makes you go and open the
    laptop, which is the thing the notification was supposed to save you.
    """
    out: list[Message] = []
    for a in plan.alerts:
        if a.level != "urgent":
            continue
        lines = [a.text]
        fix = next(
            (s for s in plan.swaps
             if s.sit and a.player_id and s.sit.player_id == a.player_id),
            None,
        )
        if fix:
            # Name the slot. The replacement is often not the same position —
            # a back going down can slide a FLEX up and pull a receiver in
            # behind him — and "start this WR instead" without the slot reads
            # as an instruction to put a receiver in a running back's spot.
            lines.append(
                f"Start {_who(fix.start)} in your {fix.slot}  (+{fix.gain:.1f})"
            )
            if fix.why:
                lines.append(f"· {fix.why[0]}")
        out.append(Message(
            title=f"Week {plan.week} · lineup problem",
            lines=lines,
            urgent=True,
            key=f"alert:{plan.week}:{a.text}",
        ))
    return out


def swap_messages(plan) -> list[Message]:
    """A start/sit call the report has newly arrived at."""
    out: list[Message] = []
    for s in plan.swaps:
        lines = [f"Start {_who(s.start)}"]
        if s.sit:
            lines.append(f"over  {_who(s.sit)}")
        lines.append(f"in {s.slot}   +{s.gain:.1f} pts")
        lines += [f"· {w}" for w in s.why[:3]]
        out.append(Message(
            title=f"Week {plan.week} · start/sit",
            lines=lines,
            key=f"swap:{plan.week}:{s.start.player_id}>{s.sit.player_id if s.sit else '-'}",
        ))
    return out


def waiver_messages(board, limit: int = 3, speculative: bool = False) -> list[Message]:
    """Only the adds worth interrupting someone for.

    A pickup you would not actually make is not news. Everything filtered out
    here still appears on the waiver board and in the digest — this only
    governs what is allowed to buzz a phone.
    """
    out: list[Message] = []
    for p in board.pickups[:limit]:
        if p.kind == "stash":
            continue
        if p.move is not None and not (speculative or p.move.actionable):
            continue
        lines = [_who(p.player)]
        lines += [f"· {w}" for w in p.why[:2]]
        lines += _move_lines(p.move)
        out.append(Message(
            title=f"Week {board.week} · waiver {p.kind}",
            lines=lines,
            key=f"waiver:{board.week}:{p.player.player_id}",
        ))
    return out


def news_messages(items, speculative: bool = False) -> list[Message]:
    """One message per thing that just changed.

    Injuries always go out — they are about your own roster, not a suggestion
    to gamble. Pickups have to earn it.
    """
    icon = {"injury": "Injury", "promotion": "Pickup", "market": "Trending"}
    out: list[Message] = []
    for i in items:
        move = getattr(i, "move", None)
        if i.kind != "injury" and move is not None:
            if not (speculative or move.actionable):
                continue
        out.append(Message(
            title=f"{icon.get(i.kind, 'News')} · {i.headline}",
            lines=list(i.detail) + _move_lines(getattr(i, "move", None)),
            urgent=i.urgent,
            key=i.key,
        ))
    return out


def digest_message(plan, board=None, brief=None) -> Message:
    """The whole week in one message, for a scheduled send."""
    lines: list[str] = []
    if brief:
        lines.append(brief.text)
        lines.append("")
    if plan.opponent:
        # Raw projections lead because they are what Sleeper shows and can be
        # checked; the model's context-adjusted view follows, labelled. Both
        # sides are the lineup you would field by following this message.
        lines.append(
            f"vs {plan.opponent.owner_name} — projected "
            f"{plan.followed_raw:.1f} to {plan.opponent_raw:.1f}"
        )
        lines.append(
            f"  model: {plan.followed_points:.1f} to {plan.opponent_points:.1f}"
            + (" with the swaps below" if plan.swaps else "")
        )
    locked = getattr(plan, "locked_starters", 0)
    if locked and locked >= len(plan.lineup.assignment):
        lines.append("  Every starter's game has kicked off — nothing left to change.")
    elif locked:
        lines.append(f"  {locked} of your starters have kicked off and are "
                     f"locked — advice covers the rest.")
    urgent = [a.text for a in plan.alerts if a.level == "urgent"]
    if urgent:
        lines.append("")
        lines.append("Fix first:")
        lines += [f"  ! {t}" for t in urgent]

    lines.append("")
    if plan.swaps:
        lines.append(f"Start/sit ({plan.current_points:.1f} -> "
                     f"{plan.followed_points:.1f}):")
        for s in plan.swaps:
            lines.append(f"  {s.start.name} over {s.sit.name if s.sit else '—'} "
                         f"in {s.slot}  +{s.gain:.1f}")
            if s.why:
                lines.append(f"    · {s.why[0]}")
    else:
        lines.append("Start/sit: lineup is already optimal.")

    if board and board.pickups:
        lines.append("")
        lines.append("Waivers:")
        for p in board.pickups[:4]:
            note = f" — {p.why[0]}" if p.why else ""
            lines.append(f"  {p.player.name} ({p.player.position}) "
                         f"~{p.horizon_points:.1f}/wk{note}")
    if plan.stale_context:
        lines.append("")
        lines.append("(context from last season — no games played yet)")
    return Message(title=f"Week {plan.week} · {plan.roster.owner_name}", lines=lines)
