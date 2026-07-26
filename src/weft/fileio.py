"""ONE transport engine for file observation, shared across the address
vocabularies (run keys, content refs, raw paths): candidate dicts in,
honest answers out. One engine means the range semantics, the vanish
guards and the positive-local dispatch cannot drift between verbs
(one-vocabulary-one-parser, applied to transport).

A candidate: {at, adapter|None, path, root} — adapter None reads a
controller-local path directly; `root` is the confining subtree,
re-asserted remote-side (defense in depth)."""

from __future__ import annotations

import base64
import os
import shlex
from pathlib import Path

from .errors import WeftError

RANGE_CAP_DEFAULT = 16 << 20        # 16 MiB per call — a transport tier
                                    # bounds ONE call, never the file


def range_cap(default: int = RANGE_CAP_DEFAULT) -> int:
    """WEFT_RANGE_READ_CAP is read PER CALL (an env var honored only
    before import is a silent no-op for embedders); malformed values
    refuse loudly — accept-and-mangle is the tested failure."""
    raw = os.environ.get("WEFT_RANGE_READ_CAP")
    if raw is None:
        return default
    try:
        cap = int(raw)
        if cap <= 0:
            raise ValueError
    except ValueError:
        raise WeftError(
            "task.invalid",
            f"WEFT_RANGE_READ_CAP must be a positive integer, got "
            f"{raw!r}", stage="infra") from None
    return cap


def stat_candidate(cand: dict) -> dict | None:
    """{bytes, mtime} of one candidate, None when absent. (Known wart,
    carried: an adapter rc!=0 reads as absent — rc-trust; tool_honesty
    row. Fix when touched with a marker protocol like stat_batch's.)"""
    path = cand["path"]
    if cand["adapter"] is None:
        p = Path(path)
        if not p.is_file():
            return None
        st = p.stat()
        return {"bytes": st.st_size, "mtime": int(st.st_mtime)}
    r = cand["adapter"].run_cmd(
        f'[ -f {shlex.quote(path)} ] && '
        f'(stat -c "%s %Y" {shlex.quote(path)} 2>/dev/null || '
        f'stat -f "%z %m" {shlex.quote(path)}) || echo ABSENT',
        timeout=60)
    out = (r.out or "").strip()
    if r.rc != 0 or out == "ABSENT" or not out:
        return None
    parts = out.split()
    return {"bytes": int(parts[0]), "mtime": int(parts[1])}


def stat_batch(adapter, paths: list[str]) -> dict[str, dict | None]:
    """ONE shell invocation stat-ing every path; per-path POSITIVE
    markers ("<idx> <bytes> <mtime>" / "<idx> ABSENT") — a missing
    marker is a broken probe and raises, never a file verdict."""
    if not paths:
        return {}
    lines = []
    for i, p in enumerate(paths):
        q = shlex.quote(p)
        lines.append(
            f'if [ -f {q} ]; then printf "%s " {i}; '
            f'(stat -c "%s %Y" {q} 2>/dev/null || '
            f'stat -f "%z %m" {q}); '
            f'else echo "{i} ABSENT"; fi')
    r = adapter.run_cmd("\n".join(lines), timeout=120)
    got: dict[int, list] = {}
    for ln in (r.out or "").splitlines():
        parts = ln.split()
        if len(parts) >= 2 and parts[0].isdigit():
            got[int(parts[0])] = parts[1:]
    missing = [i for i in range(len(paths)) if i not in got]
    if missing:
        raise WeftError(
            "internal.error",
            f"stat batch produced no marker for {len(missing)} "
            f"path(s) — probe trouble, not a file verdict",
            stage="infra", retryable=True,
            hints={"rc": r.rc, "log_tail": (r.err or r.out)[-500:]})
    out: dict[str, dict | None] = {}
    for i, p in enumerate(paths):
        g = got[i]
        out[p] = (None if g[0] == "ABSENT"
                  else {"bytes": int(g[0]), "mtime": int(g[1])})
    return out


def _is_local(adapter) -> bool:
    """POSITIVE local signal only — an adapter without a transport
    attribute (cloud wraps an inner ssh) must take the shim lane,
    never a controller-side pread of a remote path (best case untyped
    ENOENT; worst case a same-named LOCAL file answers wrong bytes)."""
    from .adapters.local import LocalAdapter
    return (adapter is None or isinstance(adapter, LocalAdapter)
            or getattr(adapter, "transport", None) == "local")


def range_read(candidates: list[dict], offset: int = 0,
               length: int | None = None, *, cap: int | None = None,
               stat_fn=stat_candidate, what: str = "file",
               missing_hints: dict | None = None) -> dict:
    """`length` bytes at `offset` from the first candidate that exists
    — pread on controller-local paths, the shim's read-from lane
    otherwise (containment re-asserted remote-side via the candidate's
    `root`). Returns {at, abs_path, offset, nbytes, size, eof, capped,
    bytes_b64}; the caller adds its addressing keys.

    offset past EOF is NOT an error (empty + eof + size — answer 416
    upstream); length above the per-call cap clamps (capped=True);
    a file vanishing between stat and read is data.missing retryable —
    never a 0-byte eof=False success a chunk streamer would spin on."""
    if cap is None:
        cap = range_cap()
    if not isinstance(offset, int) or isinstance(offset, bool) \
            or offset < 0:
        raise WeftError("task.invalid",
                        f"offset must be a non-negative int, got "
                        f"{offset!r}", stage="infra")
    if length is None:
        length = cap
    if not isinstance(length, int) or isinstance(length, bool) \
            or length < 0:
        raise WeftError("task.invalid",
                        f"length must be a non-negative int, got "
                        f"{length!r}", stage="infra")
    capped = length > cap
    want = min(length, cap)
    for cand in candidates:
        st = stat_fn(cand)
        if st is None:
            continue
        size = st["bytes"]
        if _is_local(cand["adapter"]):
            try:
                with Path(cand["path"]).open("rb") as f:
                    f.seek(offset)
                    data = f.read(want)
            except OSError as e:
                raise WeftError(
                    "data.missing",
                    f"file vanished between stat and read: {what}",
                    stage="infra", retryable=True,
                    hints={"os_error": str(e)[:200]}) from e
        else:
            r = cand["adapter"].shim(
                ["read-from", "--file", cand["path"],
                 "--offset", str(offset), "--max", str(want),
                 "--root", cand["root"], "--base64"], timeout=300)
            if r.rc == 3:
                raise WeftError(
                    "task.invalid",
                    f"path escapes its root remote-side: {what}",
                    stage="infra")
            if r.rc != 0:
                raise WeftError(
                    "data.missing",
                    f"range read failed: {(r.err or '')[:200]}",
                    stage="infra", retryable=True)
            data = base64.b64decode("".join(r.out.split()))
            if not data and want > 0 and offset < size:
                raise WeftError(
                    "data.missing",
                    f"file vanished between stat and read: {what}",
                    stage="infra", retryable=True)
        out = {"at": cand["at"], "abs_path": cand["path"],
               "offset": offset, "nbytes": len(data), "size": size,
               "eof": offset + len(data) >= size, "capped": capped,
               "bytes_b64": base64.b64encode(data).decode()}
        if "via" in cand:
            out["via"] = cand["via"]
        return out
    raise WeftError("data.missing",
                    f"no such file at any known location: {what}",
                    stage="infra", hints=missing_hints or {})
