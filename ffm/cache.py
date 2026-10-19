"""Tiny TTL cache on disk.

The Sleeper player blob is ~15MB and the projections are ~6MB; refetching
either mid-draft would stall the UI, so both are cached to disk.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

CACHE_DIR = Path(os.environ.get("FFM_CACHE_DIR", Path.home() / ".cache" / "ff-manager"))


def _path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def get(key: str, ttl_seconds: int) -> Any | None:
    p = _path(key)
    if not p.exists():
        return None
    if ttl_seconds >= 0 and time.time() - p.stat().st_mtime > ttl_seconds:
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def put(key: str, value: Any) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(key).with_suffix(".tmp")
    tmp.write_text(json.dumps(value))
    tmp.replace(_path(key))


def cached(key: str, ttl_seconds: int, producer: Callable[[], Any], *, stale_ok: bool = True) -> Any:
    """Return a cached value, else produce and store it.

    If the producer fails and a stale copy exists, the stale copy wins — a
    draft in progress is no time to hard-fail on a network blip.
    """
    hit = get(key, ttl_seconds)
    if hit is not None:
        return hit
    try:
        value = producer()
    except Exception:
        if stale_ok:
            stale = get(key, -1)
            if stale is not None:
                return stale
        raise
    put(key, value)
    return value


def clear() -> int:
    if not CACHE_DIR.exists():
        return 0
    n = 0
    for f in CACHE_DIR.glob("*.json"):
        f.unlink()
        n += 1
    return n
