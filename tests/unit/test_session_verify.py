"""ensure_available P1: verify= on session verbs, with record-gating —
records exist exactly when verification passed; the oracle runs in the
COMPOSED runtime (the same world user code sees)."""

import json

import pytest

from weft.adapters.base import ShimResult
from weft.api import Weft
from weft.realize import env_dir_rel
from weft.verify import MARKER

ENV = "env:v1:deadbeefcafe"


def _weft(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def _cold_session(tmp_path, pixi_bin):
    w = _weft(tmp_path, pixi_bin)
    w.store.put_env(ENV, "spec_" + ENV[-12:], {
        "extras": {},
        "platforms": {"any": [{"kind": "pypi", "name": "plotpkg",
                               "version": "1.0"}]},
    }, "lock: {}", "[project]", ["any"])
    rel = env_dir_rel(ENV)
    d = tmp_path / "site" / rel
    (d / "bin").mkdir(parents=True)
    (d / "activate.sh").write_text(f'export PATH="{d}/bin:$PATH"\n')
    (d / ".weft-ready").write_text(json.dumps({"strategy": "prefix"}))
    w.store.set_realization(ENV, "local", "prefix", rel, "ready",
                            read_only=True)
    r = w.session_start(ENV, "local")
    return w, r["session_id"]


def _no_toolchain(monkeypatch):
    import weft.toolchain as toolchain
    monkeypatch.setattr(toolchain, "ensure_toolchain", lambda *a, **k: None)


def _script_log(monkeypatch, w, answers):
    """Intercept the LOCAL adapter's run_cmd/run_activated: R-install
    scripts and verify oracles answered from `answers` (matched by
    substring), everything else passes through. Returns the log."""
    ad = w.adapters["local"]
    log = []
    orig_cmd, orig_act = ad.run_cmd, ad.run_activated

    def route(script, orig, timeout):
        for key, resp in answers.items():
            if key in script:
                log.append((key, script))
                return resp
        return orig(script, timeout=timeout)

    monkeypatch.setattr(ad, "run_cmd",
                        lambda s, timeout=120.0: route(s, orig_cmd, timeout))
    monkeypatch.setattr(
        ad, "run_activated",
        lambda s, timeout=120.0: route(s, orig_act, timeout))
    return log


def _rmarker(name, ok=True, got=None, kind="loads"):
    row = {"name": name, "kind": kind, "ok": ok}
    if got:
        row["got"] = got
    return MARKER + json.dumps(row)


# ── compat: without verify, nothing changes and no oracle runs ─────────────

def test_no_verify_is_byte_identical_and_oracle_free(tmp_path, pixi_bin,
                                                     monkeypatch):
    w, sid = _cold_session(tmp_path, pixi_bin)
    _no_toolchain(monkeypatch)
    log = _script_log(monkeypatch, w, {
        "install.packages": ShimResult(0, "ok", ""),
        "MISSING": ShimResult(0, "", ""),
        "WEFT-VERIFY": ShimResult(0, "", "")})
    out = w.session_install(sid, cran=["praise"])
    assert "error" not in out and "verified" not in out
    assert w.store.get_session(sid)["added_cran"] == ["praise"]
    assert not [k for k, _ in log if k == "WEFT-VERIFY"]   # zero oracles


# ── verify=True: pass records, fail retracts + raises ──────────────────────

def test_verified_install_records_and_reports(tmp_path, pixi_bin,
                                              monkeypatch):
    w, sid = _cold_session(tmp_path, pixi_bin)
    _no_toolchain(monkeypatch)
    _script_log(monkeypatch, w, {
        "install.packages": ShimResult(0, "ok", ""),
        "MISSING": ShimResult(0, "", ""),
        "WEFT-VERIFY": ShimResult(0, _rmarker("praise", got="1.0"), "")})
    out = w.session_install(sid, cran=["praise"], verify=True)
    assert out["verified"]["praise"]["status"] == "passed"
    assert w.store.get_session(sid)["added_cran"] == ["praise"]


def test_failed_postcondition_retracts_and_raises(tmp_path, pixi_bin,
                                                  monkeypatch):
    """Installed-but-not-proven: typed error, record RETRACTED — the
    snapshot must not carry a claim the oracle refuted."""
    w, sid = _cold_session(tmp_path, pixi_bin)
    _no_toolchain(monkeypatch)
    _script_log(monkeypatch, w, {
        "install.packages": ShimResult(0, "ok", ""),
        "MISSING": ShimResult(0, "", ""),
        "WEFT-VERIFY": ShimResult(0, _rmarker("praise", ok=False), "")})
    out = w.session_install(sid, cran=["praise"], verify=True)
    assert out["error"] == "env.realize_failed"
    assert out["hints"]["postcondition"] is True
    assert out["hints"]["verified"]["praise"]["status"] == "failed"
    assert out["hints"]["retracted"]["cran"] == ["praise"]
    assert out["hints"]["runtime"]                     # flip moment kept
    assert w.store.get_session(sid)["added_cran"] == []   # GATED


def test_version_pin_failure_is_wrong_version_not_wrong_install(
        tmp_path, pixi_bin, monkeypatch):
    """The wrong-package/wrong-version killer: install succeeded, the
    composed runtime holds 0.9 against a ==1.0 pin."""
    w, sid = _cold_session(tmp_path, pixi_bin)
    _no_toolchain(monkeypatch)
    _script_log(monkeypatch, w, {
        "install.packages": ShimResult(0, "ok", ""),
        "MISSING": ShimResult(0, "", ""),
        "WEFT-VERIFY": ShimResult(0, _rmarker("praise", got="0.9"), "")})
    out = w.session_install(sid, cran=["praise ==1.0"], verify=True)
    assert out["error"] == "env.realize_failed"
    v = out["hints"]["verified"]["praise"]
    assert v["status"] == "failed" and v["got"] == "0.9" \
        and v["want"] == "==1.0"
    assert w.store.get_session(sid)["added_cran"] == []


def test_unknown_oracle_withholds_record_then_late_records(
        tmp_path, pixi_bin, monkeypatch):
    """Could-not-run is unknown: success WITHOUT a record; the re-install
    converges once the oracle runs (late-record)."""
    w, sid = _cold_session(tmp_path, pixi_bin)
    _no_toolchain(monkeypatch)
    log = _script_log(monkeypatch, w, {
        "install.packages": ShimResult(0, "ok", ""),
        "MISSING": ShimResult(0, "", ""),
        "WEFT-VERIFY": ShimResult(139, "", "Segmentation fault")})
    out = w.session_install(sid, cran=["praise"], verify=True)
    assert "error" not in out
    assert out["verified"]["praise"]["status"] == "unknown"
    assert "late-record" in out["unverified_note"]
    assert w.store.get_session(sid)["added_cran"] == []   # withheld
    # oracle heals; re-install converges to a recorded, verified state
    log.clear()
    _script_log(monkeypatch, w, {
        "install.packages": ShimResult(0, "ok", ""),
        "MISSING": ShimResult(0, "", ""),
        "WEFT-VERIFY": ShimResult(0, _rmarker("praise", got="1.0"), "")})
    out2 = w.session_install(sid, cran=["praise"], verify=True)
    assert out2["verified"]["praise"]["status"] == "passed"
    assert w.store.get_session(sid)["added_cran"] == ["praise"]


def test_explicit_verify_dict_routes_by_ecosystem(tmp_path, pixi_bin,
                                                  monkeypatch):
    w, sid = _cold_session(tmp_path, pixi_bin)
    _no_toolchain(monkeypatch)
    log = _script_log(monkeypatch, w, {
        "install.packages": ShimResult(0, "ok", ""),
        "MISSING": ShimResult(0, "", ""),
        "WEFT-VERIFY": ShimResult(0, _rmarker("praise", got="2.0"), "")})
    out = w.session_install(sid, cran=["praise"],
                            verify={"versions": {"praise": ">=1.5"}})
    assert out["verified"]["praise"]["status"] == "passed"
    # the oracle ran through the COMPOSED runtime (overlay sourced)
    oracle = next(s for k, s in log if k == "WEFT-VERIFY")
    assert "overlay.sh" in oracle and "Rscript" in oracle


def test_verify_events_emitted(tmp_path, pixi_bin, monkeypatch):
    w, sid = _cold_session(tmp_path, pixi_bin)
    _no_toolchain(monkeypatch)
    _script_log(monkeypatch, w, {
        "install.packages": ShimResult(0, "ok", ""),
        "MISSING": ShimResult(0, "", ""),
        "WEFT-VERIFY": ShimResult(0, _rmarker("praise", got="1.0"), "")})
    w.session_install(sid, cran=["praise"], verify=True)
    ev = [e for e in w.store.events_since(0, 200)
          if e["kind"] == "session.verified"]
    assert ev and ev[-1]["passed"] == ["praise"]


# ── run_installer: explicit-only postcondition ─────────────────────────────

def test_run_installer_verify_must_be_explicit(tmp_path, pixi_bin,
                                               monkeypatch):
    w, sid = _cold_session(tmp_path, pixi_bin)
    _no_toolchain(monkeypatch)
    out = w.session_run_installer(sid, "true", writes_to="rlib",
                                  verify=True)
    assert out["error"] == "task.invalid"
    assert "explicit" in out["detail"]


def test_run_installer_explicit_verify_failure_raises(tmp_path, pixi_bin,
                                                      monkeypatch):
    w, sid = _cold_session(tmp_path, pixi_bin)
    _no_toolchain(monkeypatch)
    _script_log(monkeypatch, w, {
        "WEFT-VERIFY": ShimResult(0, _rmarker("widgetcore", ok=False), "")})
    out = w.session_run_installer(sid, "true", writes_to="rlib",
                                  verify={"loads": ["widgetcore"]})
    assert out["error"] == "env.realize_failed"
    assert out["hints"]["verified"]["widgetcore"]["status"] == "failed"
    assert "captured" in out["hints"]["note"]
