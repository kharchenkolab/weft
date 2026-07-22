"""ensure_available P3: the tagged-mode verb — envelope, pre-check
short-circuit + late-record, per-lane attempts, halting rules, and the
heartbeated one-ensure-per-session claim."""

import pytest

from helpers_verify import cold_session, marker, no_toolchain, script_log
from weft.adapters.base import ShimResult
from weft.errors import WeftError


# ── intake ─────────────────────────────────────────────────────────────────

def test_target_and_request_shapes_refused(tmp_path, pixi_bin):
    w, sid = cold_session(tmp_path, pixi_bin)
    for target in ("s1", {}, {"nonsense": 1}):
        r = w.ensure_available(target, {"pypi": ["idna"]})
        assert r["error"] == "task.invalid", target
    r = w.ensure_available({"env": "env:v1:x"}, {"pypi": ["idna"]})
    assert r["error"] == "task.invalid" and "later round" in r["detail"]
    for req in ("idna", {}, {"julia": ["X"]}):
        r = w.ensure_available({"session": sid}, req)
        assert r["error"] == "task.invalid", req
    r = w.ensure_available({"session": sid}, ["idna"], lanes=["conda"])
    assert r["error"] == "task.invalid" and "later round" in r["detail"]


# ── pre-check: satisfaction is checked, not assumed ────────────────────────

def test_satisfied_short_circuits_and_late_records(tmp_path, pixi_bin,
                                                   monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    log = script_log(monkeypatch, w, {
        "WEFT-VERIFY": marker("idna", got="3.7"),
        "pip install": ShimResult(0, "must not install", "")})
    out = w.ensure_available({"session": sid}, {"pypi": ["idna"]})
    assert out["satisfied"] is True and out["changed"] is False
    assert out["attempts"] == []
    assert out["verified"]["idna"]["status"] == "passed"
    assert out["runtime"]
    # LATE-RECORD: on disk but unrecorded -> recorded at the pre-check
    assert "idna" in w.store.get_session(sid)["added_pypi"]
    assert not [k for k, _ in log if k == "pip install"]   # zero installs


def test_wrong_version_present_is_not_satisfied(tmp_path, pixi_bin,
                                                monkeypatch):
    """Pins give the pre-check meaning: present-but-wrong-version
    enters the install lane."""
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    calls = {"n": 0}

    def oracle():
        calls["n"] += 1
        # pre-check sees 1.0; post-install verify sees 2.0
        return marker("idna", got="1.0" if calls["n"] == 1 else "2.0")

    script_log(monkeypatch, w, {
        "WEFT-VERIFY": oracle,
        "#method": ShimResult(0, "#method uv\nok", "")})   # fast pypi lane
    out = w.ensure_available({"session": sid}, {"pypi": ["idna ==2.0"]})
    assert out["satisfied"] is True and out["changed"] is True
    assert [a["lane"] for a in out["attempts"]] == ["pypi"]
    assert out["attempts"][0]["outcome"] == "installed"
    assert out["verified"]["idna"]["status"] == "passed"


# ── attempts: typed, halting, verbatim ─────────────────────────────────────

def test_lane_failure_is_typed_with_the_envelope(tmp_path, pixi_bin,
                                                 monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    script_log(monkeypatch, w, {
        "WEFT-VERIFY": marker("absentpkg", ok=False),
        "#method": ShimResult(
            1, "", "ERROR: No matching distribution found for absentpkg"),
        "pip install": ShimResult(
            1, "", "ERROR: No matching distribution found for absentpkg")})
    out = w.ensure_available({"session": sid}, {"pypi": ["absentpkg"]})
    assert out["error"] == "env.solve_conflict"      # classifier verbatim
    assert out["hints"]["attempts"][0]["outcome"] == "failed"
    assert out["hints"]["attempts"][0]["error"]["error"] == \
        "env.solve_conflict"
    assert out["hints"]["runtime"]                   # flip moment kept


def test_site_outage_halts_remaining_lanes(tmp_path, pixi_bin,
                                           monkeypatch):
    """An outage is not an unavailability verdict: later lanes are
    SKIPPED (halted), and the top-level code is the outage."""
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)

    def dead():
        raise WeftError("site.unreachable", "ssh transport failed",
                        stage="infra", retryable=True)

    script_log(monkeypatch, w, {
        "WEFT-VERIFY": marker("x", ok=False),
        "#method": dead, "pip install": dead,
        "install.packages": ShimResult(0, "must not run", "")})
    out = w.ensure_available({"session": sid},
                             {"pypi": ["idna"], "cran": ["praise"]})
    assert out["error"] == "site.unreachable" and out["retryable"]
    lanes = {a["lane"]: a for a in out["hints"]["attempts"]}
    assert lanes["pypi"]["outcome"] == "failed"
    assert lanes["cran"]["outcome"] == "skipped"
    assert lanes["cran"]["skip_reason"] == "halted"


def test_verify_false_is_installed_unverified(tmp_path, pixi_bin,
                                              monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    log = script_log(monkeypatch, w, {
        "WEFT-VERIFY": ShimResult(1, "must not run", ""),
        "#method": ShimResult(0, "#method uv\nok", "")})
    out = w.ensure_available({"session": sid}, {"pypi": ["idna"]},
                             verify=False)
    assert out["satisfied"] is True
    assert out["attempts"][0]["outcome"] == "installed_unverified"
    assert not [k for k, _ in log if k == "WEFT-VERIFY"]   # zero oracles


# ── the claim: one ensure per session, heartbeat semantics ─────────────────

def test_concurrent_ensure_refused_retryable(tmp_path, pixi_bin,
                                             monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    assert w.store.claim_session_ensure(sid, "other-holder")
    out = w.ensure_available({"session": sid}, {"pypi": ["idna"]})
    assert out["error"] == "state.conflict" and out["retryable"]
    assert "holder_beat_age_s" in out["hints"]
    w.store.release_session_ensure(sid, "other-holder")


def test_stale_claim_is_taken_over(tmp_path, pixi_bin, monkeypatch):
    """Beat-age staleness, never chain duration (the lease lesson)."""
    import time
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    assert w.store.claim_session_ensure(sid, "dead-holder")
    w.store._write("UPDATE sessions SET ensure_hb=? WHERE session_id=?",
                   (time.time() - 3600, sid))
    script_log(monkeypatch, w, {"WEFT-VERIFY": marker("idna", got="3.7")})
    out = w.ensure_available({"session": sid}, {"pypi": ["idna"]})
    assert out["satisfied"] is True                  # takeover worked
    assert w.store.session_ensure_claim(sid) is None   # and released


def test_ensure_events_emitted(tmp_path, pixi_bin, monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    script_log(monkeypatch, w, {
        "WEFT-VERIFY": marker("idna", got="1.0"),
        "#method": ShimResult(0, "#method uv\nok", "")})
    w.ensure_available({"session": sid}, {"pypi": ["idna"]}, verify=False)
    kinds = [e["kind"] for e in w.store.events_since(0, 300)]
    assert "session.ensure_attempt" in kinds
    assert "session.ensure_done" in kinds


def test_re_ensure_short_circuit_is_cheap(tmp_path, pixi_bin, monkeypatch):
    """Machine cadence + cost budget: a satisfied re-ensure never
    re-solves — pre-check only (<1s each, LOCAL number; per-site
    budgets are the reality run's job)."""
    import time
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    script_log(monkeypatch, w, {"WEFT-VERIFY": marker("idna", got="3.7")})
    w.ensure_available({"session": sid}, {"pypi": ["idna"]})   # warm
    t0 = time.monotonic()
    for _ in range(3):
        out = w.ensure_available({"session": sid}, {"pypi": ["idna"]})
        assert out["changed"] is False and out["attempts"] == []
    assert time.monotonic() - t0 < 3.0
