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


def sha256_shell(qpath: str) -> str:
    """The one shell recipe for hashing a file on a site (coreutils
    sha256sum, perl shasum fallback). `qpath` arrives ALREADY quoted.
    Output is the tool's raw line — callers take `${h%% *}` and must
    validate hex64 (data._require_digest doctrine: a real hash or a
    loud absence, never a fallback identity)."""
    return f"sha256sum {qpath} 2>/dev/null || shasum -a 256 {qpath}"


def sha256_batch_shell(qpaths: str) -> str:
    """The multi-file sibling: one process hashes the whole (already
    quoted, space-joined) list — the per-file variant is a fork per
    file (~1.4ms of pure spawn each; the aba2 list-tree measurement).
    Tool selection is by PRESENCE (if/else), never the single-file
    recipe's `||`: there a partial failure (one vanished file, rc!=0)
    would re-run the whole batch through the fallback tool and emit
    duplicate lines. Output is one 'HASH  PATH' line per surviving
    file; callers match by PATH, not by order, so vanished files
    become honest absences."""
    return (f"if command -v sha256sum >/dev/null 2>&1; "
            f"then sha256sum {qpaths} 2>/dev/null; "
            f"else shasum -a 256 {qpaths} 2>/dev/null; fi")


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
    """Kind-aware stat of one candidate: {"kind": "file", bytes, mtime}
    or {"kind": "dir", mtime} — no bytes for a directory (its size is
    a WALK, not a stat) — None when absent. Directories are first-class
    (the dir-as-a-unit doctrine settle_pins ratified): the old [ -f ]
    read a .zarr-class store as ABSENT and the (run, relpath) handle
    refused exactly the artifact class it matters most for (3 live
    hits). The carried rc-trust wart is PAID here: a probe that yields
    no verdict RAISES retryable — transport trouble is never a file
    verdict."""
    path = cand["path"]
    if cand["adapter"] is None:
        p = Path(path)
        if p.is_dir():
            return {"kind": "dir", "mtime": int(p.stat().st_mtime)}
        if not p.is_file():
            return None
        st = p.stat()
        return {"kind": "file", "bytes": st.st_size,
                "mtime": int(st.st_mtime)}
    q = shlex.quote(path)
    r = cand["adapter"].run_cmd(
        f'if [ -d {q} ]; then printf "DIR "; '
        f'(stat -c "%Y" {q} 2>/dev/null || stat -f "%m" {q}); '
        f'elif [ -f {q} ]; then '
        f'(stat -c "%s %Y" {q} 2>/dev/null || stat -f "%z %m" {q}); '
        f'else echo ABSENT; fi', timeout=60)
    parts = (r.out or "").split()
    if parts and parts[0] == "ABSENT":
        return None
    if len(parts) >= 2 and parts[0] == "DIR":
        return {"kind": "dir", "mtime": int(parts[1])}
    if r.rc == 0 and len(parts) >= 2:
        return {"kind": "file", "bytes": int(parts[0]),
                "mtime": int(parts[1])}
    raise WeftError(
        "internal.error",
        "stat probe produced no verdict for the path — probe trouble, "
        "never a file verdict", stage="infra", retryable=True,
        hints={"rc": r.rc, "path": path,
               "log_tail": (r.err or r.out)[-300:]})


def stat_batch(adapter, paths: list[str]) -> dict[str, dict | None]:
    """ONE shell invocation stat-ing every path; per-path POSITIVE
    markers ("<idx> <bytes> <mtime>" / "<idx> DIR <mtime>" /
    "<idx> ABSENT") — a missing marker is a broken probe and raises,
    never a file verdict. Same kind vocabulary as stat_candidate."""
    if not paths:
        return {}
    lines = []
    for i, p in enumerate(paths):
        q = shlex.quote(p)
        lines.append(
            f'if [ -d {q} ]; then printf "%s DIR " {i}; '
            f'(stat -c "%Y" {q} 2>/dev/null || stat -f "%m" {q}); '
            f'elif [ -f {q} ]; then printf "%s " {i}; '
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
            hints={"paths": [paths[i] for i in missing][:8],
                   "rc": r.rc, "log_tail": (r.err or r.out)[-500:]})
    out: dict[str, dict | None] = {}
    for i, p in enumerate(paths):
        g = got[i]
        if g[0] == "ABSENT":
            out[p] = None
        elif g[0] == "DIR":
            out[p] = {"kind": "dir", "mtime": int(g[1])}
        else:
            out[p] = {"kind": "file", "bytes": int(g[0]),
                      "mtime": int(g[1])}
    return out


def _is_local(adapter) -> bool:
    """POSITIVE local signal only — an adapter without a transport
    attribute (cloud wraps an inner ssh) must take the shim lane,
    never a controller-side pread of a remote path (best case untyped
    ENOENT; worst case a same-named LOCAL file answers wrong bytes)."""
    from .adapters.local import LocalAdapter
    return (adapter is None or isinstance(adapter, LocalAdapter)
            or getattr(adapter, "transport", None) == "local")


def _read_local(cand: dict, offset: int, want: int,
                what: str) -> tuple[int, bytes] | None:
    """(size, data) from a controller-local path; None when absent.
    Absence and read are ATOMIC (open then fstat) — there is no
    stat-then-read window on this lane."""
    try:
        with Path(cand["path"]).open("rb") as f:
            size = os.fstat(f.fileno()).st_size
            f.seek(offset)
            data = f.read(want)
        return size, data
    except FileNotFoundError:
        return None
    except OSError as e:
        raise WeftError("data.missing",
                        f"unreadable: {what}", stage="infra",
                        retryable=True,
                        hints={"os_error": str(e)[:200]}) from e


def _read_shim(cand: dict, offset: int, want: int,
               what: str) -> tuple[int, bytes] | None:
    """(size, data) through the shim's ATOMIC with-size lane; None when
    absent. A response with neither SIZE nor ABSENT marker is a broken
    probe (internal.error retryable) — never a file verdict."""
    r = cand["adapter"].shim(
        ["read-from", "--file", cand["path"],
         "--offset", str(offset), "--max", str(want),
         "--root", cand["root"], "--base64", "--with-size"],
        timeout=300)
    if r.rc == 3:
        raise WeftError("task.invalid",
                        f"path escapes its root remote-side: {what}",
                        stage="infra")
    if r.rc != 0:
        raise WeftError("data.missing",
                        f"range read of {what} failed: "
                        f"{(r.err or '')[:200]}",
                        stage="infra", retryable=True,
                        hints={"what": what})
    lines = (r.out or "").splitlines()
    head = lines[0].split() if lines else []
    if head[:1] == ["ABSENT"]:
        return None
    if len(head) != 2 or head[0] != "SIZE":
        raise WeftError(
            "internal.error",
            f"range read produced no marker — probe trouble, not a "
            f"file verdict: {what}",
            stage="infra", retryable=True,
            hints={"log_tail": (r.out or r.err)[:300]})
    size = int(head[1])
    data = base64.b64decode("".join("".join(lines[1:]).split()))
    return size, data


def _read_candidate(cand: dict, offset: int, want: int,
                    what: str) -> tuple[int, bytes] | None:
    if _is_local(cand["adapter"]):
        return _read_local(cand, offset, want, what)
    return _read_shim(cand, offset, want, what)


def range_read(candidates: list[dict], offset: int = 0,
               length: int | None = None, *, cap: int | None = None,
               what: str = "file",
               missing_hints: dict | None = None) -> dict:
    """`length` bytes at `offset` from the first candidate that exists
    — pread on controller-local paths, the shim's ATOMIC with-size
    lane otherwise (one round trip: absence-or-read, no separate
    pre-stat, no stat-then-read race). Returns {at, abs_path, offset,
    nbytes, size, eof, capped, bytes_b64}; the caller adds its
    addressing keys.

    offset past EOF is NOT an error (empty + eof + size — answer 416
    upstream); length above the per-call cap clamps (capped=True); an
    absent candidate falls through to the next."""
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
        got = _read_candidate(cand, offset, want, what)
        if got is None:
            continue
        size, data = got
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


def range_read_many(cands_by_key: dict, *, cap: int | None = None,
                    missing_hints: dict | None = None) -> dict:
    """Batched WHOLE-member reads: {key: [candidates]} in, {key:
    entry | typed-error-dict} + not_read out — ONE remote invocation
    per (adapter, root) group (the per-call round-trip floor is the
    WAN cost; a chunk cascade must not pay it N times).

    The cap bounds the CALL's total payload: members past it come back
    in `not_read` (an explicit remainder — the caller loops; never a
    silent truncation). Absent-at-primary keys fall back through their
    remaining candidates via singular reads. Local candidates pread
    directly (no round trips to save)."""
    if cap is None:
        cap = range_cap()
    files: dict = {}
    not_read: list = []
    budget = cap

    def _emit(key, cand, size, data):
        nonlocal budget
        budget -= len(data)
        entry = {"at": cand["at"], "abs_path": cand["path"],
                 "offset": 0, "nbytes": len(data), "size": size,
                 "eof": len(data) >= size, "capped": False,
                 "bytes_b64": base64.b64encode(data).decode()}
        if "via" in cand:
            entry["via"] = cand["via"]
        files[key] = entry

    # group each key's FIRST candidate; remote groups batch, local
    # candidates read inline
    groups: dict = {}
    for key, cands in cands_by_key.items():
        if not cands:
            files[key] = WeftError(
                "data.missing", f"no known location: {key}",
                stage="infra").to_dict()
            continue
        cand = cands[0]
        if _is_local(cand["adapter"]):
            groups.setdefault(("__local__",), []).append((key, cand))
        else:
            groups.setdefault(
                (id(cand["adapter"]), cand["root"]),
                []).append((key, cand))

    fallback: list = []          # (key,) needing per-key candidate walk
    for gkey, members in groups.items():
        if gkey == ("__local__",):
            for key, cand in members:
                if budget <= 0:
                    not_read.append(key)
                    continue
                got = _read_local(cand, 0, budget, str(key))
                if got is None:
                    fallback.append(key)
                    continue
                size, data = got
                if len(data) < size:        # bigger than the budget
                    not_read.append(key)
                    continue
                _emit(key, cand, size, data)
            continue
        adapter = members[0][1]["adapter"]
        root = members[0][1]["root"]
        if budget <= 0:
            not_read.extend(k for k, _ in members)
            continue
        import uuid as _uuid
        plan_rel = f"tmp/readmulti-{_uuid.uuid4().hex[:10]}.txt"
        adapter.write_file(
            plan_rel,
            ("\n".join(c["path"] for _, c in members) + "\n").encode())
        try:
            r = adapter.shim(
                ["read-multi", "--plan", adapter.path(plan_rel),
                 "--root", root, "--max-total", str(budget)],
                timeout=600)
        finally:
            adapter.run_cmd(
                f"rm -f {shlex.quote(adapter.path(plan_rel))}",
                timeout=60)
        if r.rc == 3:
            raise WeftError("task.invalid",
                            "a path escapes its root remote-side",
                            stage="infra")
        if r.rc != 0:
            raise WeftError("data.missing",
                            f"batched read failed: "
                            f"{(r.err or '')[:200]}",
                            stage="infra", retryable=True)
        # parse the %%W frames, in plan order
        seen: dict[int, tuple] = {}
        cur_idx, cur_size, buf = None, None, []
        for ln in (r.out or "").splitlines():
            if ln.startswith("%%W "):
                if cur_idx is not None:
                    seen[cur_idx] = (cur_size, "".join(buf))
                parts = ln.split()
                cur_idx, buf = int(parts[1]), []
                if parts[2] == "SIZE":
                    cur_size = int(parts[3])
                else:
                    seen[cur_idx] = (parts[2], "")   # ABSENT | DEFER
                    cur_idx = None
            elif cur_idx is not None:
                buf.append(ln.strip())
        if cur_idx is not None:
            seen[cur_idx] = (cur_size, "".join(buf))
        for i, (key, cand) in enumerate(members):
            got = seen.get(i)
            if got is None:
                raise WeftError(
                    "internal.error",
                    f"batched read produced no marker for entry {i} — "
                    f"probe trouble, not a file verdict",
                    stage="infra", retryable=True,
                    hints={"log_tail": (r.out or r.err)[-300:]})
            mark, payload = got
            if mark == "ABSENT":
                fallback.append(key)
            elif mark == "DEFER":
                not_read.append(key)
            else:
                data = base64.b64decode(payload)
                if len(data) != mark:
                    raise WeftError(
                        "internal.error",
                        f"batched read size mismatch for entry {i}",
                        stage="infra", retryable=True)
                _emit(key, cand, mark, data)

    # absent at the primary: walk the remaining candidates singly
    for key in fallback:
        rest = cands_by_key[key][1:]
        try:
            got = range_read(rest, 0, None, cap=max(budget, 1),
                             what=str(key),
                             missing_hints=missing_hints)
            files[key] = got
            budget -= got["nbytes"]
        except WeftError as e:
            files[key] = e.to_dict()
    return {"files": files, "not_read": not_read}
