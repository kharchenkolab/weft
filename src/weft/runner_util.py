"""Small pure helpers shared by the runner and the poller."""

from __future__ import annotations

from .errors import WeftError


def walltime_to_s(t: str) -> float | None:
    """THE walltime grammar — slurm's --time, verbatim, because the
    string is handed to #SBATCH --time verbatim. Weft had three parsers
    with three answers (2026-07 vocabulary sweep #4): the poller
    CRASHED on slurm-legal '1-00:00:00'/'infinite', the capability
    check read 'D-HH' hours as seconds, and a bare integer meant
    seconds to weft but MINUTES to slurm.

    MM | MM:SS | HH:MM:SS | D-HH | D-HH:MM | D-HH:MM:SS | infinite.
    None = no limit."""
    t = (t or "").strip().lower()
    if not t or t in ("infinite", "unlimited", "n/a"):
        return None
    try:
        if "-" in t:
            d, rest = t.split("-", 1)
            parts = [int(p) for p in rest.split(":")]
            while len(parts) < 3:
                parts.append(0)          # D-HH pads RIGHT: hours first
            h, m, s = parts[:3]
            return int(d) * 86400 + h * 3600 + m * 60 + s
        parts = [int(p) for p in t.split(":")]
        if len(parts) == 1:
            return parts[0] * 60         # bare integer = MINUTES (slurm)
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]          # MM:SS
        h, m, s = parts[-3:]
        return h * 3600 + m * 60 + s
    except ValueError:
        raise WeftError(
            "task.invalid", f"cannot parse walltime {t!r}",
            stage="submit",
            hints={"grammar": "MM | MM:SS | HH:MM:SS | D-HH | D-HH:MM "
                              "| D-HH:MM:SS | infinite"})


def parse_walltime(w: str) -> float | None:
    return walltime_to_s(w)


def du_apparent_bytes_cmd(path_quoted: str) -> str:
    """Shell snippet printing a directory's apparent size in bytes.
    GNU du has -sb; BSD/darwin needs -A -sk (apparent KB). `grep .` turns
    GNU-absent empty output into a failure so the fallback fires."""
    return (f"du -sb {path_quoted} 2>/dev/null | cut -f1 | grep . || "
            f"du -A -sk {path_quoted} 2>/dev/null | awk '{{print $1 * 1024}}'")
