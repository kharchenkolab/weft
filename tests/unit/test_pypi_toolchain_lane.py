"""bug5 A1 — the pypi session lanes summon weft's toolchain LAZILY.

Motivating incident (cbe-next prj_77a8beea, weft 699c350): a pypi
session install hit an sdist and died with
    error: [Errno 2] No such file or directory: 'g++'
— ensure_toolchain had exactly ONE caller in the tree (the cran
session lane) while every pypi install lane ran bare pip/uv. Each
lane now retries ONCE with the toolchain prelude when the failure
wears a compile signature (env.realize_failed + compile marker);
solve and network verdicts never pay for a toolchain. The gate is one
owner (_pypi_build_retry_prelude); the signature classifier rides the
stderr corpus (tests/fixtures/stderr_corpus/pypi_sdist_missing_gxx.txt
is the incident verbatim)."""

import json
from pathlib import Path

from helpers_verify import cold_session, no_toolchain, script_log
from weft.adapters.base import ShimResult

CORPUS = Path(__file__).parent.parent / "fixtures" / "stderr_corpus"

GXX_TAIL = (CORPUS / "pypi_sdist_missing_gxx.txt").read_text()
NET_TAIL = ("WARNING: Retrying... Could not fetch URL "
            "https://pypi.org/simple/statpack/: "
            "NewConnectionError('connection refused')\n")
TC_MARK = "/fake/tc/.pixi/envs/default/bin"


def fake_toolchain(monkeypatch, calls=None):
    import weft.toolchain as tc

    def ens(adapter, pixi_bin, platform, extra_deps=(), *, emit=None):
        if calls is not None:
            calls.append({"platform": platform,
                          "extra_deps": tuple(extra_deps)})
        return "/fake/tc"

    monkeypatch.setattr(tc, "ensure_toolchain", ens)


# ── the signature classifier ───────────────────────────────────────────────

def test_compile_signature_families():
    from weft.evidence import compile_signature
    assert compile_signature(GXX_TAIL), "the incident verbatim"
    assert compile_signature("error: command '/usr/bin/g++' failed "
                             "with exit code 1")
    assert compile_signature("unable to execute 'gcc': No such file "
                             "or directory")
    assert compile_signature("Failed building wheel for statpack")
    assert compile_signature("  × Failed to build `statpack==1.0`")
    assert compile_signature("fatal error: zlib.h: No such file or "
                             "directory"), "syslib class is a subset"
    assert not compile_signature(NET_TAIL)
    assert not compile_signature("ResolutionImpossible: for help visit")
    assert not compile_signature("")


# ── the gate (one owner for all four lanes) ────────────────────────────────

def test_gate_refuses_wrong_verdict_and_missing_signature(
        tmp_path, pixi_bin, monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    fake_toolchain(monkeypatch)
    s = w.store.get_session(sid)
    ad = w.adapters["local"]
    sm = w.sessions
    assert sm._pypi_build_retry_prelude(s, ad, "env.solve_failed",
                                        GXX_TAIL) == "", \
        "a network verdict must not pay for a toolchain"
    assert sm._pypi_build_retry_prelude(s, ad, "env.realize_failed",
                                        NET_TAIL) == "", \
        "no compile signature, no retry"
    p = sm._pypi_build_retry_prelude(s, ad, "env.realize_failed",
                                     GXX_TAIL)
    assert TC_MARK in p and "\n" not in p, "inline prelude"
    kinds = [e["kind"] for e in w.store.events_since(0, 300)]
    assert "session.toolchain_retry" in kinds


def test_gate_threads_session_build_deps(tmp_path, pixi_bin,
                                         monkeypatch):
    """Parity with the cran lane: the session's build_deps extend the
    toolchain prefix (the lzma.h class) on the pypi retry too."""
    w, sid = cold_session(tmp_path, pixi_bin)
    calls = []
    fake_toolchain(monkeypatch, calls)
    w.store.set_session_build_deps(sid, ["xz"])
    s = w.store.get_session(sid)
    p = w.sessions._pypi_build_retry_prelude(
        s, w.adapters["local"], "env.realize_failed", GXX_TAIL)
    assert p and calls[-1]["extra_deps"] == ("xz",)


def test_gate_degrades_when_toolchain_unavailable(tmp_path, pixi_bin,
                                                  monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    s = w.store.get_session(sid)
    assert w.sessions._pypi_build_retry_prelude(
        s, w.adapters["local"], "env.realize_failed", GXX_TAIL) == ""
    kinds = [e["kind"] for e in w.store.events_since(0, 300)]
    assert "session.toolchain_retry" not in kinds, \
        "no retry happened — no retry event"


# ── the pylib lanes, behaviorally ──────────────────────────────────────────

def _flaky_build(first_tail, second=ShimResult(0, "installed ok", "")):
    """First call dies with first_tail; the retry succeeds."""
    seen = []

    def resp():
        seen.append(1)
        return ShimResult(1, first_tail, "") if len(seen) == 1 else second

    return resp, seen


def test_old_pip_lane_retries_with_prelude(tmp_path, pixi_bin,
                                           monkeypatch):
    """The full-closure --target fallback (pip without --report): a
    compile death earns one retry whose script carries the toolchain
    prelude AFTER activation."""
    w, sid = cold_session(tmp_path, pixi_bin)
    fake_toolchain(monkeypatch)
    resp, seen = _flaky_build(GXX_TAIL)
    log = script_log(monkeypatch, w, {
        "--dry-run": ShimResult(1, "no such option: --report", ""),
        "pip install --no-input": resp,
    })
    out = w.session_install(sid, pypi=["statpack"])
    assert "error" not in out, out
    assert len(seen) == 2, "exactly one retry"
    retry_script = [s for k, s in log
                    if k == "pip install --no-input"][1]
    assert TC_MARK in retry_script
    assert retry_script.index("activate.sh") \
        < retry_script.index(TC_MARK), \
        "activation FIRST, prelude AFTER (the shell-hook PATH reset)"
    assert "-pypi-target-tc.log" in retry_script, \
        "the retry persists its own log — both survive"


def test_phase_b_retry_and_verdict_survival(tmp_path, pixi_bin,
                                            monkeypatch):
    """Phase B (--no-deps at pins): compile death -> toolchain retry
    -> success; the happy-path bookkeeping (fetch_method) reads the
    RETRY's output."""
    w, sid = cold_session(tmp_path, pixi_bin)
    fake_toolchain(monkeypatch)
    s = w.store.get_session(sid)
    report = (tmp_path / "site" / s["location"] / "pip-report.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(
        {"install": [{"metadata": {"name": "statpack",
                                   "version": "1.0"}}]}))
    resp, seen = _flaky_build(
        GXX_TAIL, second=ShimResult(0, "#fetch pip", ""))
    log = script_log(monkeypatch, w, {
        "--dry-run": ShimResult(0, "", ""),
        "#fetch uv": resp,      # the phase-B fetch script's marker
    })
    out = w.session_install(sid, pypi=["statpack"])
    assert "error" not in out, out
    assert len(seen) == 2
    assert out["fetch_method"] == "pip", \
        "bookkeeping keyed on the retry's output"
    retry_script = [s2 for k, s2 in log if k == "#fetch uv"][1]
    assert TC_MARK in retry_script
    kinds = [e["kind"] for e in w.store.events_since(0, 300)]
    assert "session.toolchain_retry" in kinds


def test_network_failure_never_retries(tmp_path, pixi_bin,
                                       monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    fake_toolchain(monkeypatch)
    s = w.store.get_session(sid)
    report = (tmp_path / "site" / s["location"] / "pip-report.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(
        {"install": [{"metadata": {"name": "statpack",
                                   "version": "1.0"}}]}))
    calls = []

    def resp():
        calls.append(1)
        return ShimResult(1, NET_TAIL, "")

    script_log(monkeypatch, w, {
        "--dry-run": ShimResult(0, "", ""),
        "#fetch uv": resp,
    })
    out = w.session_install(sid, pypi=["statpack"])
    assert out["error"] == "env.solve_failed"
    assert len(calls) == 1, "network verdicts pay for NO toolchain"


def test_unprovisionable_toolchain_keeps_original_error(
        tmp_path, pixi_bin, monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    calls = []

    def resp():
        calls.append(1)
        return ShimResult(1, GXX_TAIL, "")

    script_log(monkeypatch, w, {
        "--dry-run": ShimResult(1, "no such option: --report", ""),
        "pip install --no-input": resp,
    })
    out = w.session_install(sid, pypi=["statpack"])
    assert out["error"] == "env.realize_failed"
    assert len(calls) == 1
    assert "log_path" in out["hints"], "evidence survives the degrade"


def test_all_four_lanes_route_through_the_gate():
    """Drift-catcher (NOT coverage — the behavioral tests above are):
    every pypi install site in session.py consults the one-owner gate.
    The four sites: old-pip --target, phase-B fetch, pixi add, the
    pylib->clone replay."""
    src = (Path(__file__).parent.parent.parent / "src" / "weft"
           / "session.py").read_text()
    assert src.count("_pypi_build_retry_prelude(") >= 5, \
        "4 call sites + the def — a removed site is a regressed lane"
