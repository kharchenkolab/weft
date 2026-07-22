"""P4 enumeration harness — written BEFORE the ranked executor; these
invariants ARE its spec. Every per-lane outcome sequence (up to 3
lanes) is generated against scripted lanes and the invariants are
asserted on every path — orchestration bugs hide from examples, not
from enumeration."""

import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "unit"))
from helpers_verify import cold_session, no_toolchain
from weft.errors import WeftError
from weft.session import SessionManager

LANES3 = ["conda", "pypi", "cran"]
OUTCOMES = ["installed", "verify_failed", "refused", "lane_failed",
            "site_dead", "internal"]

HALTING = {"site_dead", "internal"}


def _err(kind, lane):
    if kind == "verify_failed":
        return WeftError("env.realize_failed",
                         f"postcondition failed in {lane}",
                         stage="realize",
                         hints={"postcondition": True,
                                "verified": {"pkgx": {
                                    "status": "failed",
                                    "check": "metadata",
                                    "got": "0.1", "want": ">=1"}}})
    if kind == "refused":
        return WeftError("session.cold_base", f"{lane} refused",
                         stage="realize",
                         hints={"options": {"full_clone": True}})
    if kind == "lane_failed":
        return WeftError("env.solve_conflict", f"{lane} cannot provide",
                         stage="solve", hints={"lane": lane})
    if kind == "site_dead":
        return WeftError("site.unreachable", "transport died",
                         stage="infra", retryable=True)
    return WeftError("internal.error", "a weft bug", stage="realize")


def _rig(monkeypatch, w, sid, script):
    """script: {lane: outcome}. Fakes install per the P1 contract and
    the oracles: pre-check always misses; final verify passes iff some
    lane installed."""
    calls = []

    def fake_install(self, session_id, adapter, conda=None, pypi=None,
                     cran=None, verify=None, **kw):
        lane = "conda" if conda else ("pypi" if pypi else "cran")
        calls.append(lane)
        kind = script[lane]
        if kind == "installed":
            return {"installed": {lane: (conda or pypi or cran)},
                    "verified": {"pkgx": {"status": "passed",
                                          "check": "metadata",
                                          "got": "2.0"}},
                    "session_id": session_id}
        raise _err(kind, lane)

    monkeypatch.setattr(SessionManager, "install", fake_install)

    def fake_exec_fn(self, s, adapter):
        def run(script_text, timeout):
            from weft.adapters.base import ShimResult
            from weft.verify import MARKER
            import json
            # the pre-check always MISSES so every chain runs
            return ShimResult(0, MARKER + json.dumps(
                {"name": "pkgx", "kind": "metadata", "ok": False,
                 "reason": "not installed"}), "")
        return run

    monkeypatch.setattr(SessionManager, "_verify_exec_fn", fake_exec_fn)
    return calls


@pytest.fixture(scope="module")
def rig_session(tmp_path_factory, pixi_bin):
    tmp = tmp_path_factory.mktemp("chain")
    return cold_session(tmp, pixi_bin)


def _run(w, sid, lanes):
    return w.ensure_available({"session": sid}, ["pkgx"], lanes=lanes)


def _events_for(w, sid, since):
    return [e for e in w.store.events_since(since, 500)
            if e["kind"] == "session.ensure_attempt"]


@pytest.mark.parametrize("n_lanes", [1, 2, 3])
def test_every_outcome_sequence(rig_session, monkeypatch, n_lanes):
    w, sid = rig_session
    no_toolchain(monkeypatch)
    lanes = LANES3[:n_lanes]
    for combo in itertools.product(OUTCOMES, repeat=n_lanes):
        script = dict(zip(lanes, combo))
        with monkeypatch.context() as mp:
            calls = _rig(mp, w, sid, script)
            seq = w.store.events_since(0, 1)  # cursor
            last = w.store._row("SELECT MAX(seq) AS m FROM events")["m"] or 0
            out = _run(w, sid, lanes)

            # reconstruct the EXPECTED executed prefix
            executed, halted, success = [], False, False
            for ln in lanes:
                executed.append(ln)
                k = script[ln]
                if k == "installed":
                    success = True
                    break
                if k in HALTING:
                    halted = True
                    break

            if "error" in out:
                atts = out["hints"]["attempts"]
            else:
                atts = out["attempts"]
            ran = [a for a in atts if a["outcome"] != "skipped"]
            skipped = [a for a in atts if a["outcome"] == "skipped"]

            # 1: attempts == executed lanes, in order; install calls agree
            assert [a["lane"] for a in ran] == executed, script
            assert calls == executed, script
            # 2: nothing runs after success or a halting failure
            if success:
                assert not skipped and "error" not in out, script
                assert out["satisfied"] is True
            if halted:
                assert out["error"] == _err(script[executed[-1]],
                                            "x").code, script
                assert out["error"] != "env.unavailable_in_lanes"
                assert all(a["skip_reason"] == "halted"
                           for a in skipped), script
                assert len(atts) == len(lanes), script
            # 3: exhaustion iff every lane ran to a lane-scoped verdict
            if not success and not halted:
                assert out["error"] == "env.unavailable_in_lanes", script
                assert len(ran) == len(lanes)
            # 4: no laundering — injected errors ride verbatim
            for a in ran:
                k = script[a["lane"]]
                if k == "installed":
                    assert a["outcome"] == "installed"
                else:
                    want = _err(k, a["lane"])
                    assert a["outcome"] == (
                        "refused" if k == "refused" else "failed"), script
                    assert a["error"]["error"] == want.code, script
                    assert a["error"]["detail"] == want.detail, script
            # 5: events mirror the attempts one-to-one
            ev = _events_for(w, sid, last)
            assert [e["lane"] for e in ev] == [a["lane"] for a in atts], \
                script
