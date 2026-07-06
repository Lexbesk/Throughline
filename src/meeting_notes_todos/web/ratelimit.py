"""In-memory rate limiting (v4 M19 hardening).

A per-process fixed-window counter keyed by client IP + bucket. Deliberately
lightweight — no Redis — which is appropriate for a small closed-group app: it
throttles brute-force and hammering, and if Fly runs more than one machine the
limits apply per machine (approximate, which is fine at this scale).

Enforcement is gated on the ``THROUGHLINE_RATE_LIMIT`` environment flag so local
dev and the test suite are unaffected; production sets it (see ``fly.toml``).
"""

from __future__ import annotations

import time

# (max requests, window seconds) per bucket. Auth is strict (brute-force
# defense); the general API bucket is generous enough for a human clicking
# around but blocks scripted hammering.
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "auth": (10, 60),
    "api": (240, 60),
}

# paths that use the strict auth bucket
AUTH_PATHS = ("/api/login", "/api/password")


class RateLimiter:
    """Fixed-window counter. ``allow`` returns False once a key exceeds its limit
    within the current window; the window resets after ``window`` seconds."""

    def __init__(self) -> None:
        self._hits: dict[str, tuple[float, int]] = {}

    def allow(self, key: str, limit: int, window: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        start, count = self._hits.get(key, (now, 0))
        if now - start >= window:  # window elapsed → reset
            start, count = now, 0
        count += 1
        self._hits[key] = (start, count)
        return count <= limit

    def clear(self) -> None:
        self._hits.clear()


def bucket_for(path: str) -> str:
    return "auth" if path in AUTH_PATHS else "api"
