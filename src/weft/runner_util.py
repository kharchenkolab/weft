"""Small pure helpers shared by the runner and the poller."""

from __future__ import annotations


def parse_walltime(w: str) -> float | None:
    if not w:
        return None
    parts = [int(p) for p in w.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3:]
    return h * 3600 + m * 60 + s
