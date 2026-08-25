"""evidence.py is the one owner of failure forensics for long build
lanes: full-log persistence (run_logged), marker-anchored error
regions, and the syslib classifier. Every ERROR_MARKERS family gets
its own test — a marker without a test is decoration (the channel_hint
lesson). The extractor cases replay the r-signac thread's three causal
lines, each of which sat OUTSIDE the old tail window."""

from pathlib import Path

from weft.adapters.local import LocalAdapter
from weft.evidence import (ERROR_MARKERS, HEAD_BYTES, TAIL_BYTES,
                           extract_error_regions, failure_evidence,
                           run_logged)


def _markers(regions):
    return [r["marker"] for r in regions]


# ---- one test per marker family (the r-signac trio first) -------------

def test_marker_r_dep_unavailable():
    got = extract_error_regions(
        "* installing *source* package ...\n"
        "dependencies 'toolpkgA', 'toolpkgB' are not available for "
        "package 'toolpkgC'\n")
    assert _markers(got) == ["r_dep_unavailable"]


def test_marker_compiler_missing():
    got = extract_error_regions(
        "x86_64-conda-linux-gnu-c++: command not found\n")
    assert _markers(got) == ["compiler_missing"]


def test_marker_syslib_header():
    got = extract_error_regions(
        "compat.c:5:10: fatal error: zlib.h: No such file or directory\n")
    assert _markers(got) == ["syslib"]


def test_marker_make_error():
    got = extract_error_regions(
        "make: *** [Makefile:14: Signac.ts] Error 1\n")
    assert _markers(got) == ["make_error"]


def test_marker_r_nonzero():
    got = extract_error_regions(
        "ERROR: compilation failed for package 'toolpkg'\n"
        "installation of package 'toolpkg' had non-zero exit status\n")
    assert "r_nonzero" in _markers(got)


def test_marker_pip_error():
    got = extract_error_regions(
        "ERROR: Could not find a version that satisfies the "
        "requirement nosuchdist\n"
        "No matching distribution found for nosuchdist\n")
    assert "pip_error" in _markers(got)


def test_marker_julia_error():
    got = extract_error_regions(
        "Unsatisfiable requirements detected for package Foo\n")
    assert _markers(got) == ["julia_error"]


def test_marker_resource():
    got = extract_error_regions(
        "tar: write failed: No space left on device\n")
    assert _markers(got) == ["resource"]


# ---- extractor mechanics ---------------------------------------------

def test_regions_carry_context_and_merge_overlaps():
    text = ("line0\nline1\n"
            "make: *** [Makefile:2: a.o] Error 1\n"
            "make: *** [Makefile:3: b.o] Error 2\n"
            "line4\nline5\n")
    got = extract_error_regions(text)
    assert len(got) == 1                       # overlap merged
    assert "line1" in got[0]["lines"]          # before-context
    assert "line4" in got[0]["lines"]          # after-context
    assert "Error 2" in got[0]["lines"]        # both hits present


def test_region_budget_is_reported_not_silent():
    chunks = []
    for i in range(20):
        chunks += ["x" * 30, f"make: *** [Makefile:{i}: t{i}] Error 1",
                   "y" * 30, "z" * 30]
    text = "\n".join(chunks)
    got = extract_error_regions(text, before=0, after=0, max_regions=3)
    assert _markers(got)[:3] == ["make_error"] * 3
    assert got[-1]["marker"] == "truncated"
    assert "log_path" in got[-1]["lines"]


def test_clean_log_has_no_regions():
    assert extract_error_regions("downloading ...\nall good\n") == []


# ---- run_logged + failure_evidence against a REAL local adapter ------

def _adapter(tmp_path) -> LocalAdapter:
    a = LocalAdapter("evidence-test", Path(tmp_path) / "site-root")
    (Path(tmp_path) / "site-root").mkdir(parents=True, exist_ok=True)
    return a


def test_run_logged_persists_the_full_log_site_side(tmp_path):
    """The r-signac gap: NO install log survived under the site root
    (site_exec searched; none). run_logged's log outlives the run and
    holds ALL output, not a window."""
    a = _adapter(tmp_path)
    r = run_logged(a, "echo early-causal-line; seq 1 20000; "
                      "echo 'make: *** [Makefile:14: t.o] Error 1' >&2; "
                      "exit 3",
                   "logs/realize-test.log", timeout=60)
    assert r.rc == 3                            # rc is preserved
    log = Path(a.path("logs/realize-test.log"))
    assert log.exists()
    full = log.read_text()
    assert full.startswith("early-causal-line") # head survived in FULL
    assert "Error 1" in full                    # stderr interleaved
    assert "early-causal-line" not in r.out     # ...but NOT in the tail
    assert "20000" in r.out                     # the tail is the tail
    assert len(r.out.encode()) <= TAIL_BYTES


def test_failure_evidence_reaches_head_of_log(tmp_path):
    """The causal 'dependencies are not available' line prints before
    the first download — a tail can never contain it; evidence reads
    the head too."""
    a = _adapter(tmp_path)
    r = run_logged(a, "echo \"dependencies 'toolpkgA' are not available "
                      "for package 'toolpkgB'\"; seq 1 20000; exit 1",
                   "logs/realize-head.log", timeout=60)
    ev = failure_evidence(a, "logs/realize-head.log", r.out)
    assert ev["log_path"].endswith("logs/realize-head.log")
    assert len(ev["log_tail"]) <= 6000
    assert "r_dep_unavailable" in _markers(ev["error_regions"])
    assert HEAD_BYTES >= 4096                   # sanity on the window


def test_failure_evidence_survives_a_dead_adapter(tmp_path):
    """Evidence gathering must never replace the real failure with its
    own: a broken head-read degrades to tail-only regions."""
    a = _adapter(tmp_path)

    def broken_run_cmd(*args, **kw):
        raise RuntimeError("transport died")
    a.run_cmd = broken_run_cmd
    ev = failure_evidence(a, "logs/gone.log",
                          "make: *** [Makefile:1: x] Error 1")
    assert ev["log_path"].endswith("logs/gone.log")
    assert _markers(ev["error_regions"]) == ["make_error"]


def test_syslib_hints_reexported_from_session():
    """session.py's seven call sites and the historical import path
    keep working; evidence.py is the one owner now."""
    from weft.evidence import _syslib_hints as canonical
    from weft.session import _syslib_hints as legacy
    assert legacy is canonical
