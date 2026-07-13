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


def du_apparent_bytes_cmd(path_quoted: str) -> str:
    """Shell snippet printing a directory's apparent size in bytes.
    GNU du has -sb; BSD/darwin needs -A -sk (apparent KB). `grep .` turns
    GNU-absent empty output into a failure so the fallback fires."""
    return (f"du -sb {path_quoted} 2>/dev/null | cut -f1 | grep . || "
            f"du -A -sk {path_quoted} 2>/dev/null | awk '{{print $1 * 1024}}'")
