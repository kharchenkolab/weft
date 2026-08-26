"""Failure evidence that OUTLIVES the failing process — one owner.

The motivating incident (aba2 th594060f7 item 1): a 3-hour cran
install failed three times; its full output existed only in controller
memory, the payload carried a fixed-size tail, and the causal lines —
"dependencies ... are not available" near the HEAD, "c++: command not
found" and "zlib.h: No such file" mid-stream — all fell outside the
window. No log survived anywhere for site_exec to find; ~12 of the
thread's 23 wasted minutes were re-deriving what the log had said.
lock.py has persisted full solve stderr next to its tail since the
"forensics survive a swallowed exception" incident — this module
carries that pattern to every long lane, plus the two readers the log
needs:

- run_logged():          run a site command with FULL output persisted
                         site-side under <root>/logs/; the returned
                         tail is a WINDOW onto a log that exists,
                         never the only copy.
- extract_error_regions(): marker-anchored context windows — a tail is
                         positional, the causal line is not.
- _syslib_hints():       the missing-system-library classifier, moved
                         here from session.py so the realize lanes
                         (which had ZERO of its seven call sites) can
                         finally use it. session.py re-exports it.

Pinned by tests/unit/test_evidence.py (one test per marker family —
a marker without a test is decoration).
"""

from __future__ import annotations

import re
import shlex

# ---------------------------------------------------------------- markers

# The missing-SYSTEM-library subclass of a broken build pulls a
# categorically different agent lever (aba check-in item 3): retrying
# the same lane fails identically — the remedy is build_deps or an
# isolated env with a full solve. Markers are LC_ALL=C-stable
# compiler/linker/configure/pkg-config shapes. Seed set is
# conservative and grows via the ledger, never speculation.
_SYSLIB_PATTERNS: tuple[tuple[str | None, re.Pattern], ...] = (
    # gcc:   fatal error: png.h: No such file or directory
    # clang: fatal error: 'png.h' file not found
    ("header", re.compile(r"fatal error: '?([\w./+-]+\.h)'?"
                          r"(?:: No such file or directory|"
                          r" file not found)")),
    ("header", re.compile(r"([\w./+-]+\.h): No such file or directory")),
    ("library", re.compile(r"cannot find -l([\w.+-]+)")),        # GNU ld
    ("library", re.compile(r"library not found for -l([\w.+-]+)")),
    ("library", re.compile(r"ld: library '([\w.+-]+)' not found")),
    ("library", re.compile(r"error while loading shared libraries: "
                           r"lib([\w.+-]+)\.so")),
    ("pkg_config", re.compile(r"No package '([\w.+-]+)' found")),
    (None, re.compile(r"configure: error")),   # class only, no name
)


# The broader COMPILE-STAGE signature (bug5 A1): superset of the
# syslib class — a missing compiler, a dead sdist/wheel build, a
# setup.py subprocess failure. Gates the lazy retry-with-toolchain in
# the realize and session pypi lanes: a network or solve failure must
# never pay a toolchain build plus a full re-install, while every
# build-shaped death deserves one retry with weft's compilers on PATH.
# Marker families ride the stderr corpus lane
# (tests/fixtures/stderr_corpus/) — append the verbatim output of any
# misclassification incident there.
_COMPILE_PATTERNS: tuple[re.Pattern, ...] = (
    # setuptools/distutils spawning a missing tool:
    #   error: [Errno 2] No such file or directory: 'g++'
    #   unable to execute 'gcc': No such file or directory
    re.compile(r"No such file or directory: "
               r"'(?:cc|c\+\+|gcc|g\+\+|clang|clang\+\+|gfortran|"
               r"make|cmake|ninja|pkg-config)'"),
    re.compile(r"unable to execute '[^']+'"),
    # generic build-backend subprocess death:
    #   error: command '/usr/bin/g++' failed with exit code 1
    re.compile(r"error: command '[^']+' failed"),
    # pip / uv build-stage verdicts (absent from solve/network output)
    re.compile(r"Failed building wheel for \S+"),
    re.compile(r"Failed to build `?[\w.-]+"),
)


def compile_signature(text: str) -> bool:
    """Does this log show a COMPILE-stage failure? One owner for the
    retry-with-toolchain gates (realize prefix, session pypi lanes)."""
    if _syslib_hints(text) is not None:
        return True
    return any(rx.search(text) for rx in _COMPILE_PATTERNS)


def _syslib_hints(text: str) -> dict | None:
    """Scan a BUILD-failure log for the missing-system-library shape.
    Returns hint keys to merge (failure_class + captured names +
    remedy), or None — callers apply this ONLY to env.realize_failed
    verdicts, never to solve/network codes, so stray compiler text in
    an outage log cannot re-class the failure."""
    found: dict[str, str] = {}
    hit = False
    for kind, rx in _SYSLIB_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        hit = True
        if kind and kind not in found:
            found[kind] = m.group(1)
    if not hit:
        return None
    # attached from SESSION and REALIZE lanes alike — the remedy names
    # each surface's OWN lever (the old text named only
    # session_install(build_deps=), unreachable from env_realize/
    # task_submit/kernel_start; bug5-A2 sweep)
    out = {"failure_class": "missing_system_lib",
           "remedy": "a system library is missing on this site — "
                     "retrying the same lane fails identically. In a "
                     "session: session_install(build_deps="
                     "[\"<conda pkg>\"]) serves its headers/libs to "
                     "source compiles without touching the base (e.g. "
                     "[\"xz\"] for lzma.h). In an env spec: add the "
                     "package to deps.conda — the compiled .so links "
                     "it at runtime, so it is a real dependency. Or "
                     "mint an isolated env with a full solve "
                     "(extends_env / ensure_available env target)"}
    if found:
        out["missing_system"] = found
    return out


# Error-region anchors: every family below reproduces a line class that
# sat OUTSIDE the tail window in a real failure (the r-signac thread
# for the first five; the rest from the flake/tool-honesty ledgers).
# Adding a marker means adding its test in test_evidence.py.
ERROR_MARKERS: tuple[tuple[str, re.Pattern], ...] = (
    # R: install.packages prints this near the very START of the run
    ("r_dep_unavailable",
     re.compile(r"dependenc\w+ .{0,200}are not available|"
                r"package.{0,80} is not available")),
    ("pip_error",
     re.compile(r"error: subprocess-exited-with-error|"
                r"No matching distribution found|"
                r"ERROR: Could not find a version")),
    ("julia_error",
     re.compile(r"Unsatisfiable requirements detected|"
                r"ERROR: LoadError|Error building")),
    # AFTER the specific families: ^ERROR: is a catch-all
    ("r_nonzero",
     re.compile(r"installation of package .{0,120} had non-zero exit "
                r"status|^ERROR: ", re.M)),
    ("compiler_missing",
     re.compile(r"[\w+.-]*(?:g\+\+|gcc|cc|c\+\+|gfortran|clang|make|"
                r"cmake|ld): (?:command not found|No such file)")),
    ("syslib", re.compile("|".join(rx.pattern for _, rx in
                                   _SYSLIB_PATTERNS))),
    ("make_error", re.compile(r"make(?:\[\d+\])?: \*\*\* .* Error \d+")),
    ("resource",
     re.compile(r"No space left on device|Disk quota exceeded|"
                r"Permission denied|Killed(?:\s|$)")),
)


def extract_error_regions(text: str, before: int = 2, after: int = 2,
                          max_regions: int = 8,
                          max_chars: int = 4000) -> list[dict]:
    """Marker-anchored context windows over a build log.

    Returns [{"marker": <family>, "lines": <context block>}, ...] in
    log order, overlapping windows merged (one region, first marker's
    name). Bounded by max_regions/max_chars — and the BOUND IS
    REPORTED: when regions are dropped, the last entry is a
    {"marker": "truncated", "lines": "<n> more ..."} row, never a
    silent cap."""
    lines = text.splitlines()
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        for name, rx in ERROR_MARKERS:
            if rx.search(line):
                hits.append((i, name))
                break
    regions: list[dict] = []
    spent = 0
    last_end = -1
    dropped = 0
    for i, name in hits:
        lo, hi = max(0, i - before), min(len(lines), i + after + 1)
        if regions and lo <= last_end:
            # overlap: extend the previous region instead of duplicating
            prev = regions[-1]
            add = "\n".join(lines[last_end + 1:hi])
            if add and spent + len(add) <= max_chars:
                prev["lines"] += "\n" + add
                spent += len(add)
            last_end = max(last_end, hi - 1)
            continue
        block = "\n".join(lines[lo:hi])
        if len(regions) >= max_regions or spent + len(block) > max_chars:
            dropped += 1
            continue
        regions.append({"marker": name, "lines": block})
        spent += len(block)
        last_end = hi - 1
    if dropped:
        regions.append({"marker": "truncated",
                        "lines": f"{dropped} more marker hit(s) beyond "
                                 f"the region budget — read log_path"})
    return regions


# ------------------------------------------------------------- run_logged

TAIL_BYTES = 65536
HEAD_BYTES = 32768


def run_logged(adapter, script: str, log_rel: str, timeout: int,
               runner=None):
    """Run `script` on the site with FULL output persisted to
    <root>/<log_rel>; the returned result's .out is the last TAIL_BYTES
    of that log (its .err is empty — the log interleaves both streams,
    like every jobdir log). The log survives the process: an agent's
    site_exec / run_file-style read can see what the payload window
    could not.

    `runner` defaults to adapter.run_cmd; pass adapter.run_activated
    for lanes that need login-shell semantics (activation itself stays
    IN the caller's script, as today)."""
    runner = runner or adapter.run_cmd
    log_q = shlex.quote(adapter.path(log_rel))
    dir_q = shlex.quote(adapter.path(log_rel.rsplit("/", 1)[0]))
    # subshell, NOT a brace group: an `exit N` inside the caller's
    # script must end the SCRIPT, not the wrapper (a brace group shares
    # the shell — tail would never run and the tail window would be
    # silently empty exactly on failures)
    wrapped = (f"mkdir -p {dir_q} && ( {script} ) > {log_q} 2>&1; "
               f"_weft_rc=$?; tail -c {TAIL_BYTES} {log_q}; "
               f"exit $_weft_rc")
    return runner(wrapped, timeout=timeout)


def failure_evidence(adapter, log_rel: str, tail_text: str,
                     tail_chars: int = 6000) -> dict:
    """The hint block every long-lane failure carries: where the FULL
    log lives, a bounded tail, and the marker-anchored error regions.
    Reads the log's HEAD too (one extra bounded command, failure path
    only) — R prints dependency-availability errors before the first
    download, structurally out of any tail's reach."""
    head = ""
    try:
        r = adapter.run_cmd(
            f"head -c {HEAD_BYTES} {shlex.quote(adapter.path(log_rel))}",
            timeout=60)
        if r.rc == 0:
            head = r.out or ""
    except Exception:   # noqa: BLE001 — evidence gathering must never
        pass            # replace the real failure with its own
    joined = head + "\n" + tail_text if head else tail_text
    return {"log_path": adapter.path(log_rel),
            "log_tail": tail_text[-tail_chars:],
            "error_regions": extract_error_regions(joined)}
