"""The evidence wiring, driven through the REAL lane functions with a
real LocalAdapter and stub tools (stub tool + real adapter + real
helper — the adopt-only fixture rule: only the tooling the lane lacks
gets stubbed). Each test asserts the three-part contract on failure:
the FULL log exists site-side, the payload names it (log_path), and
the marker-anchored error_regions carry the causal line a tail would
have missed."""

import os
from pathlib import Path

import pytest

from weft.adapters.local import LocalAdapter
from weft.errors import WeftError

def _site(tmp_path) -> LocalAdapter:
    root = Path(tmp_path) / "site-root"
    (root / "bin").mkdir(parents=True)
    return LocalAdapter("wiring-test", root)


def _stub_pixi(root: Path, lines: list[str], rc: int = 1) -> None:
    """A pixi that prints a build transcript and fails: the lane runs
    for real, only the tool is fake."""
    p = root / "bin" / "pixi"
    body = "#!/bin/sh\n" + "\n".join(f"echo {l!r}" for l in lines) + \
        f"\nseq 1 3000\nexit {rc}\n"
    p.write_text(body)
    os.chmod(p, 0o755)


def test_build_prefix_failure_carries_evidence(tmp_path):
    """The census's LONGEST dark lane (90-min timeout, 2000-char tail,
    nothing persisted): a syslib line early in the transcript must land
    in error_regions and the full log must exist under <root>/logs."""
    from weft.realize import _build_prefix
    a = _site(tmp_path)
    _stub_pixi(a._root, [
        "downloading packages ...",
        "compat.c:5:10: fatal error: zlib.h: No such file or directory",
    ])
    env_row = {"manifest": "[workspace]\n", "native_lock": "",
               "canonical": {"platforms": {}}}
    events = []
    with pytest.raises(WeftError) as ei:
        _build_prefix("env:v1:test", env_row, a, "envs/test", [],
                      emit=lambda kind, **kw: events.append(kind))
    e = ei.value
    assert e.code == "env.realize_failed"
    assert e.hints["log_path"].endswith("logs/test-prefix.log")
    full = Path(e.hints["log_path"]).read_text()
    assert "zlib.h" in full and "3000" in full          # FULL log
    assert any(r["marker"] == "syslib"
               for r in e.hints["error_regions"])
    assert "realize.prefix" in events                    # start event


def test_toolchain_failure_is_no_longer_invisible(tmp_path):
    """Strictly the worst pre-census shape: rc!=0 -> return None with
    the output DISCARDED. The None contract stands (callers degrade on
    purpose) but the log persists and a toolchain.failed event carries
    the evidence."""
    from weft.toolchain import ensure_toolchain
    a = _site(tmp_path)
    _stub_pixi(a._root, [
        "solving toolchain ...",
        "tar: write failed: No space left on device",
    ])
    events = []
    got = ensure_toolchain(
        a, str(a._root / "bin" / "pixi"), "linux-64",
        emit=lambda kind, **kw: events.append((kind, kw)))
    assert got is None                                   # contract kept
    kinds = [k for k, _ in events]
    assert "toolchain.failed" in kinds
    kw = [kw for k, kw in events if k == "toolchain.failed"][0]
    assert Path(kw["log_path"]).exists()
    assert "No space left" in Path(kw["log_path"]).read_text()
    assert any(r["marker"] == "resource" for r in kw["error_regions"])


def test_cran_realize_failure_persists_log_and_regions(tmp_path):
    """The 12-minute item itself: the cran realize lane's failure now
    names a log that EXISTS (site_exec found none in the field) and its
    regions carry the causal line."""
    from weft.solvers import CranSolver
    a = _site(tmp_path)
    layer = {"records": [], "top_level": ["toolpkg"],
             "snapshot": "https://example.invalid/cran/2026-01-01",
             "repos": None}
    # no activate.sh exists -> the very first line of the script fails
    # with a shell error; the retry loop sees a frozen (absent) library
    # and raises. The point under test is the EVIDENCE, not R.
    solver = CranSolver(str(a._root / "bin" / "pixi"))
    with pytest.raises(WeftError) as ei:
        solver.realize_layer(layer, a, "envs/crantest")
    e = ei.value
    assert e.code == "env.realize_failed"
    assert e.hints["log_path"].endswith(
        "logs/crantest-cran-realize.log")
    assert Path(e.hints["log_path"]).exists()
    assert "log_tail" in e.hints and "error_regions" in e.hints
    # the note is GATED on the evidence (remedy census): a shell error
    # with no network markers must NOT get the air-gapped steer that
    # misdirected the r-signac agent
    assert "air-gapped" not in e.hints["note"]
    assert "error_regions" in e.hints["note"]
