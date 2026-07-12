"""Cheap log classification: regex signatures, not models (doc 05 §4).

Extracts the probable proximate error from a log tail so the relevant
kilobyte — not 200 MB — reaches the agent's context.
"""

from __future__ import annotations

import re

_SIGNATURES: list[tuple[str, re.Pattern]] = [
    ("python-traceback", re.compile(r"Traceback \(most recent call last\):")),
    ("python-module-missing", re.compile(r"ModuleNotFoundError|ImportError")),
    ("oom-killed", re.compile(r"\bKilled\b|Out [Oo]f [Mm]emory|MemoryError|oom-kill")),
    ("command-not-found", re.compile(r"command not found|No such file or directory")),
    ("mpi-abort", re.compile(r"MPI_ABORT|MPICH ERROR|PMIX ERROR")),
    ("permission-denied", re.compile(r"Permission denied")),
    ("disk-full", re.compile(r"No space left on device")),
    ("cuda-error", re.compile(r"CUDA (error|out of memory)|cudaError")),
    ("segfault", re.compile(r"Segmentation fault|SIGSEGV")),
    ("walltime", re.compile(r"DUE TO TIME LIMIT|CANCELLED AT .* DUE TO TIME")),
]


def classify_log(tail: str) -> dict:
    """Return {signature, excerpt} for the most informative match."""
    lines = tail.splitlines()
    found: list[tuple[str, int]] = []
    for sig, pat in _SIGNATURES:
        for i, line in enumerate(lines):
            if pat.search(line):
                found.append((sig, i))
                break
    if not found:
        return {"signature": "unclassified", "excerpt": "\n".join(lines[-15:])}

    # prefer a python traceback excerpt (its *end* names the exception)
    sig, idx = found[0]
    if sig == "python-traceback":
        excerpt = "\n".join(lines[idx : idx + 40][-40:])
        # trailing lines after the traceback body rarely help; cap at the
        # last non-empty line that looks like the exception message
        return {"signature": sig, "excerpt": excerpt}
    start = max(0, idx - 3)
    return {"signature": sig, "excerpt": "\n".join(lines[start : idx + 5])}
