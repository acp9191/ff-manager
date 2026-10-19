"""Optional: turn the computed ledger into a couple of sentences.

The model is a writer, not an analyst. Every number and every reason it is
given has already been computed, checked and bounded by `data/context.py`; its
only job is to say which of them is the actual story. It never sees the raw
player pool, so it has nothing to speculate about, and it is never in the loop
that decides anything — pull the API key and the report is identical except
for one paragraph.

Three things keep it honest:

* **No invented numbers.** Every figure in the output must appear in the
  input, or the paragraph is thrown away and the mechanical ledger printed
  instead. A summary that quietly invents "he's averaging 14 a game" is worse
  than no summary, because you cannot tell by reading it.
* **No invented vocabulary.** Numbers alone are not enough. The payload is
  dense with figures, so a fabricated claim can borrow one by coincidence —
  "11.4 targets a game" once passed the numeric check because 11.4 happened to
  be another player's projected points. So a stat word the input never uses is
  also grounds for rejection.
* **No repeat billing.** The watcher polls every thirty minutes and the week
  usually has not changed, so responses are cached against a hash of the exact
  payload. An unchanged week costs one call, not one per poll.

What this does not catch is a true statement drawn incorrectly — the guards
are structural, not semantic, and a model determined to mislead using only
supplied numbers could still do it. That is the reason the summary is
decoration: nothing downstream reads it, and every number in it appears in the
tables underneath, where it can be checked.

Failure here is never fatal: no key, no network, a refusal, a timeout or a
failed guard all return None and the caller falls back to the ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass

import requests

from ffm import cache

DEFAULT_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_BASE = "https://openrouter.ai/api/v1"
TIMEOUT = 30
CACHE_TTL = 7 * 24 * 3600

SYSTEM = (
    "You write one short paragraph for a fantasy football manager's weekly "
    "report. You are given facts that have already been computed and verified. "
    "Rules, all strict:\n"
    "- Use ONLY the facts given. Never add a statistic, projection, opinion, "
    "injury, or piece of news that is not in the input.\n"
    "- Never invent or derive a number. Only cite numbers that appear in the "
    "input, and cite them unchanged.\n"
    "- Do not predict outcomes or say who will win.\n"
    "- Say which one or two things actually matter this week and why they "
    "point the same way. Do not list everything; the report already lists it.\n"
    "- 2-3 sentences, plain text, no bullet points, no headings, no markdown, "
    "no preamble like 'Here is'. Address the manager as 'you'."
)


@dataclass
class Brief:
    text: str
    model: str
    cached: bool = False


def configured() -> bool:
    return bool(os.environ.get("OPENROUTER_API_KEY"))


def payload_for(plan, board=None, adds: int = 3) -> dict:
    """The only thing the model is allowed to see: already-computed facts."""
    out: dict = {
        "week": plan.week,
        "your_team": plan.roster.owner_name,
        "your_projected_points": plan.followed_points,
        "points_if_you_change_nothing": plan.current_points,
        # Include every figure already computed, even derivable ones. The
        # guard rejects any number not in this payload, so omitting a fair one
        # does not prevent the model citing it — it just gets the paragraph
        # thrown away for saying something true.
        "points_left_on_your_bench": plan.gain,
        "urgent_problems": [a.text for a in plan.alerts if a.level == "urgent"],
        "warnings": [a.text for a in plan.alerts if a.level != "urgent"],
        "recommended_changes": [
            {
                "start": s.start.name,
                "position": s.start.position,
                "in_slot": s.slot,
                "instead_of": s.sit.name if s.sit else None,
                "points_gained": s.gain,
                "reasons": list(s.why[:4]),
            }
            for s in plan.swaps
        ],
    }
    if plan.opponent:
        out["opponent"] = plan.opponent.owner_name
        out["opponent_projected_points"] = plan.opponent_points
    # Key names are part of the vocabulary the guard checks against, so they
    # avoid stat words: a key called "waiver_targets" silently authorised the
    # model to talk about targets.
    if board and board.pickups:
        out["waiver_pickups"] = [
            {
                "player": p.player.name,
                "position": p.player.position,
                "kind": p.kind,
                "points_per_week": p.horizon_points,
                "lineup_upgrade_per_week": p.horizon_gain,
                "reasons": list(p.why[:2]),
                "drop": p.drop.name if p.drop else None,
            }
            for p in board.pickups[:adds]
        ]
    if plan.stale_context:
        out["note"] = (
            "The season has no completed games yet, so context is drawn from "
            "last season and is weaker than usual."
        )
    return out


_NUM = re.compile(r"\d+(?:\.\d+)?")

# Stat vocabulary the payload may or may not contain. Checking numbers alone
# is not enough: the payload is full of figures, so an invented claim can
# borrow one by coincidence — "11.4 targets a game" passed a numeric check
# because 11.4 was some other player's projected points. If the input never
# mentions targets, the summary must not either.
_STAT_WORDS = re.compile(
    r"\b(?:target|carr|rush|recept|catch|yard|completion|attempt|touchdown|"
    r"snap|route|sack|interception|fumble|red[- ]zone|air[- ]yard)\w*",
    re.I,
)


# How far a quoted figure may sit from a supplied one. Wide enough for 7.82
# printed as 7.8, and no wider: an earlier version allowed whole-number
# rounding, which let an invented "26.5 point swing" pass because some
# defense in the payload had allowed 26 points a game.
_NUM_TOLERANCE = 0.05


def invented_numbers(text: str, source: str) -> list[str]:
    """Numbers in the output that were not supplied in the input.

    Deliberately strict. A rejected paragraph costs nothing — the computed
    reasons print instead — while an accepted fabrication is unnoticeable.
    """
    known = [float(n) for n in _NUM.findall(source)]
    bad = []
    for tok in _NUM.findall(text):
        v = float(tok)
        if any(abs(v - k) <= _NUM_TOLERANCE for k in known):
            continue
        bad.append(tok)
    return bad


def unsupported_terms(text: str, source: str) -> list[str]:
    """Stat words used in the output that the input never mentions."""
    src = source.lower()
    out = set()
    for m in _STAT_WORDS.finditer(text):
        word = m.group(0).lower()
        stem = word[:5]
        if word not in src and stem not in src:
            out.add(word)
    return sorted(out)


# The app never claims to know who wins, and a summary that does would undo
# that in one sentence. The prompt forbids it; this is the backstop.
_PREDICTION = re.compile(
    r"\b(?:will|should|going to|gonna)\s+(?:\w+\s+){0,2}"
    r"(?:win|lose|beat|blow out|cover)\b|\b(?:guaranteed|a lock|can't lose|"
    r"cannot lose|comfortably)\b",
    re.I,
)


def predicts_outcome(text: str) -> bool:
    return bool(_PREDICTION.search(text))


def _key(payload: dict, model: str) -> str:
    blob = json.dumps(payload, sort_keys=True) + model + SYSTEM
    return "brief_" + hashlib.sha256(blob.encode()).hexdigest()[:24]


def summarize(payload: dict, *, model: str | None = None,
              use_cache: bool = True) -> Brief | None:
    """One paragraph, or None if it cannot be produced safely."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return None
    model = model or os.environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL
    base = os.environ.get("OPENROUTER_BASE_URL", DEFAULT_BASE).rstrip("/")

    key = _key(payload, model)
    if use_cache:
        hit = cache.get(key, CACHE_TTL)
        if hit:
            return Brief(text=hit["text"], model=hit.get("model", model), cached=True)

    facts = json.dumps(payload, indent=1, sort_keys=True)
    try:
        r = requests.post(
            f"{base}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                # OpenRouter uses these for its own attribution listings.
                "HTTP-Referer": "https://github.com/local/ff-manager",
                "X-Title": "ff-manager",
            },
            json={
                "model": model,
                "temperature": 0.2,
                "max_tokens": 300,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": facts},
                ],
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        body = r.json()
    except (requests.RequestException, ValueError):
        return None

    try:
        text = (body["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return None
    if not text:
        return None

    # The guards that make this safe to read. A paragraph that cites a number
    # nobody computed, or describes a statistic that was never supplied, is
    # discarded rather than shown — an unverifiable summary is worse than none,
    # because you cannot tell by reading it.
    if (
        invented_numbers(text, facts)
        or unsupported_terms(text, facts)
        or predicts_outcome(text)
    ):
        return None

    text = " ".join(text.split())
    if use_cache:
        cache.put(key, {"text": text, "model": model})
    return Brief(text=text, model=model, cached=False)


def for_report(plan, board=None, model: str | None = None) -> Brief | None:
    return summarize(payload_for(plan, board), model=model)


def list_models(filter_text: str = "") -> list[dict]:
    """Available OpenRouter models, so the id need not be guessed."""
    r = requests.get(f"{DEFAULT_BASE}/models", timeout=TIMEOUT)
    r.raise_for_status()
    rows = (r.json() or {}).get("data") or []
    if filter_text:
        rows = [m for m in rows if filter_text.lower() in m.get("id", "").lower()]
    return sorted(rows, key=lambda m: m.get("id", ""))
