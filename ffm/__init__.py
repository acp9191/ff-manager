"""ff-manager."""

from __future__ import annotations

import hashlib
from pathlib import Path

__version__ = "0.1.0"


def build_id() -> str:
    """Short content hash of the installed package.

    A deploy that silently ships old code is the worst kind of bug: every
    step reports success, the container is healthy, and the only symptom is
    a feature quietly not being there. This makes two installs comparable in
    one command — if `ffm version` differs between your checkout and the
    image, the image is stale, whatever the build log said.
    """
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for f in sorted(root.rglob("*.py")):
        h.update(f.relative_to(root).as_posix().encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:12]
