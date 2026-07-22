"""The ensure_available envelope is a PINNED cross-repo contract
(documentation/ensure_envelope.schema.json; aba mirrors this guard).
Real envelopes from the verb are validated structurally — drift fails
loudly HERE, not silently in the field."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "unit"))
from helpers_verify import cold_session, marker, no_toolchain, script_log
from weft.adapters.base import ShimResult

ATTEMPT_OUTCOMES = {"installed", "installed_unverified", "failed",
                    "refused", "skipped", "solved"}
VERIFY_STATUSES = {"passed", "failed", "unknown"}


def _check_attempt(a):
    assert a["lane"] in {"conda", "pypi", "cran", "installer",
                     "extends_env"}
    assert a["outcome"] in ATTEMPT_OUTCOMES
    if a["outcome"] == "skipped":
        assert a["skip_reason"] in {"halted", "budget", "grammar"}
    else:
        assert isinstance(a["seconds"], (int, float))
    if a["outcome"] in ("failed", "refused"):
        err = a["error"]
        for k in ("error", "stage", "detail", "retryable", "hints"):
            assert k in err, (k, err)


def _check_verified(v):
    for name, r in v.items():
        assert r["status"] in VERIFY_STATUSES, name
        assert "check" in r


def check_success(env):
    assert env["satisfied"] is True
    assert isinstance(env["changed"], bool)
    assert isinstance(env["attempts"], list)
    for a in env["attempts"]:
        _check_attempt(a)
    _check_verified(env["verified"])
    assert ("session_id" in env and env["runtime"]) or \
        "env_id" in env


def check_error(env):
    for k in ("error", "stage", "detail", "retryable", "hints"):
        assert k in env
    h = env["hints"]
    assert isinstance(h["attempts"], list) and h["attempts"]
    for a in h["attempts"]:
        _check_attempt(a)
    _check_verified(h.get("verified") or {})
    assert "runtime" in h


def test_success_envelopes_validate(tmp_path, pixi_bin, monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    script_log(monkeypatch, w, {"WEFT-VERIFY": marker("idna", got="3.7")})
    check_success(w.ensure_available({"session": sid},
                                     {"pypi": ["idna"]}))
    script_log(monkeypatch, w, {
        "WEFT-VERIFY": marker("idna", got="3.7"),
        "#method": ShimResult(0, "#method uv\nok", "")})
    check_success(w.ensure_available({"session": sid}, {"pypi": ["idna"]},
                                     verify=False))


def test_error_envelope_validates(tmp_path, pixi_bin, monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    script_log(monkeypatch, w, {
        "WEFT-VERIFY": marker("absentpkg", ok=False),
        "#method": ShimResult(
            1, "", "ERROR: No matching distribution found for absentpkg"),
        "pip install": ShimResult(
            1, "", "ERROR: No matching distribution found for absentpkg")})
    out = w.ensure_available({"session": sid}, {"pypi": ["absentpkg"]})
    check_error(out)


def test_schema_file_exists_and_versioned():
    import json
    schema = json.loads(
        (Path(__file__).parent.parent.parent / "documentation" /
         "ensure_envelope.schema.json").read_text())
    assert schema["envelope_version"] == 1
    assert set(schema["attempt"]["outcome"].split("|")) == ATTEMPT_OUTCOMES


def test_env_target_verify_now_envelopes_validate(tmp_path, pixi_bin,
                                                  monkeypatch):
    """The 2026-07-22 additive shapes (coordinated with aba's vendored
    schema copy): env-target with site= — verified_site + populated
    verified on the live path, absent + empty verified on the deferred
    path; the note is the human twin of that discriminator."""
    from helpers_verify import ENV
    w, sid = cold_session(tmp_path, pixi_bin)
    monkeypatch.setattr(w, "env_ensure", lambda spec: {"env_id": ENV})
    script_log(monkeypatch, w, {"WEFT-VERIFY": marker("plotpkg")})
    live = w.ensure_available({"env": ENV}, {"pypi": ["plotpkg"]},
                              site="local")
    check_success(live)
    assert live["verified_site"] == "local" and live["verified"]
    monkeypatch.setattr(w, "env_ensure",
                        lambda spec: {"env_id": "env:v1:fresh"})
    deferred = w.ensure_available({"env": ENV}, {"pypi": ["plotpkg"]},
                                  site="local")
    check_success(deferred)
    assert "verified_site" not in deferred and deferred["verified"] == {}


def test_ranked_cran_repositories_attempt_validates(tmp_path, pixi_bin,
                                                    monkeypatch):
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    from weft.session import SessionManager
    monkeypatch.setattr(
        SessionManager, "install",
        lambda self, session_id, adapter, cran=None, verify=None, **kw: {
            "installed": {"cran": cran},
            "verified": {"pkgA": {"status": "passed", "check": "loads"}},
            "session_id": session_id})
    out = w.ensure_available({"session": sid}, ["pkgA"], lanes=["cran"],
                             cran_repos=["https://r.example.org"])
    check_success(out)
    att = next(a for a in out["attempts"] if a["lane"] == "cran")
    assert att["repositories"] == ["https://r.example.org"]
