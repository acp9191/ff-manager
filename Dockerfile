# syntax=docker/dockerfile:1.7
#
# Python 3.14 (current stable; 3.15 is not out) and uv 0.12.9, both pinned.
# The base is the 3.14 series rather than an exact patch so a rebuild picks up
# CPython security fixes; uv is pinned exactly because it is what resolves the
# dependency tree, and that should never move on its own.
#
# Two stages so the runtime image carries no build tooling and no uv binary.
# Dependencies are installed before the source is copied, so editing ffm/ does
# not re-resolve or re-download anything.

FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer: changes only when the lockfile does.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Project layer. --no-editable installs a real copy into the venv rather than
# a link back to /app/ffm, so the runtime image needs the venv and nothing
# else — one copy of the code in the image instead of two that can drift.
#
# --reinstall-package is load-bearing, not belt-and-braces. The uv cache mount
# survives between builds, and the project version rarely changes, so uv will
# happily reuse a wheel it built days ago: source changes get copied into the
# builder, ignored, and the image ships the old code with no error anywhere.
# That is exactly what happened once, and it is invisible unless you go
# looking, because everything reports success.
COPY ffm ./ffm
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --reinstall-package ff-manager


FROM python:3.14-slim

# tzdata is not optional here. Digest times like --digest-at 09:00 are local,
# and without it TZ is silently ignored and every schedule runs on UTC.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FFM_CACHE_DIR=/data \
    TZ=UTC

# /data holds the disk cache AND the watch state — the dedupe keys and the
# injury snapshot. Lose it and every alert you have already seen is sent again,
# so it is a volume, not a scratch directory.
RUN useradd --create-home --uid 10001 ffm \
    && mkdir -p /data \
    && chown -R ffm:ffm /data

WORKDIR /app
COPY --from=builder --chown=ffm:ffm /app/.venv /app/.venv

USER ffm
VOLUME ["/data"]

# A wedged poller is still a running process, so uptime proves nothing. This
# asks the only question that matters: did a pass actually complete recently?
HEALTHCHECK --interval=10m --timeout=15s --start-period=5m --retries=2 \
    CMD ffm health --max-age 5400 || exit 1

ENTRYPOINT ["ffm"]
CMD ["--help"]
