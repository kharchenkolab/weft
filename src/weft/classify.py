"""Cheap log classification: regex signatures, not models (doc 05 §4).

Extracts the probable proximate error from a log tail so the relevant
kilobyte — not 200 MB — reaches the agent's context.
"""

from __future__ import annotations

import re

# ordered most-specific first: the primary signature is the earliest entry
# here that matches anywhere in the tail; generic shapes come last
_SIGNATURES: list[tuple[str, re.Pattern]] = [
    ("oom-killed", re.compile(r"\bKilled\b|Out [Oo]f [Mm]emory|MemoryError|oom-kill")),
    ("walltime", re.compile(r"DUE TO TIME LIMIT|CANCELLED AT .* DUE TO TIME")),
    ("cuda-error", re.compile(r"CUDA (error|out of memory)|cudaError")),
    ("segfault", re.compile(r"Segmentation fault|SIGSEGV")),
    ("mpi-abort", re.compile(r"MPI_ABORT|MPICH ERROR|PMIX ERROR")),
    ("disk-full", re.compile(r"No space left on device")),
    ("permission-denied", re.compile(r"Permission denied")),
    ("python-module-missing", re.compile(r"ModuleNotFoundError|ImportError")),
    ("command-not-found", re.compile(r"command not found|No such file or directory")),
    ("python-traceback", re.compile(r"Traceback \(most recent call last\):")),
]


def classify_log(tail: str) -> dict:
    """{signature, all_signatures, excerpt} for the most informative match."""
    lines = tail.splitlines()
    found: list[tuple[str, int]] = []
    for sig, pat in _SIGNATURES:
        for i, line in enumerate(lines):
            if pat.search(line):
                found.append((sig, i))
                break
    if not found:
        return {"signature": "unclassified", "all_signatures": [],
                "excerpt": "\n".join(lines[-15:])}

    sig, idx = found[0]
    # a traceback, when present, is the best excerpt regardless of which
    # signature wins — its end names the actual exception
    tb = next((i for s, i in found if s == "python-traceback"), None)
    if tb is not None:
        excerpt = "\n".join(lines[tb : tb + 40][-40:])
    else:
        excerpt = "\n".join(lines[max(0, idx - 3) : idx + 5])
    out = {"signature": sig, "all_signatures": [s for s, _ in found],
           "excerpt": excerpt}
    # the failed allocation size, when the runtime names it (numpy et al.);
    # bare MemoryError carries none — hints must not pretend otherwise
    m = re.search(r"[Uu]nable to allocate ([\d.]+\s*[KMGTP]iB)", tail)
    if m:
        out["failed_allocation"] = m.group(1)
    return out
