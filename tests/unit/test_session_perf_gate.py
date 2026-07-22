"""Perf round commit A (aba benchmarks note): the overlay lane gates on
WRITE-NEED, not base temperature — a warm-base pypi-only add lays a
pylib layer with NO ~10s clone; conda-after-pylib upgrades explicitly
(clone + replay, crash-safe ordering); cross-device clones say so."""

import json

import pytest

from helpers_verify import ENV, cold_session, no_toolchain
from weft.adapters.base import ShimResult
from weft.api import Weft
from weft.realize import env_dir_rel


def warm_session(tmp_path, pixi_bin):
    """Same harness as cold_session but the realization was BUILT here
    (read_only=0, strategy prefix) — the warm shape that has never
    exercised the pylib lane before this round."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
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
                            read_only=False)                # WARM
    r = w.session_start(ENV, "local")
    return w, r["session_id"]


def _pylib_fakes(monkeypatch, w, sid, tmp_path, extra=None):
    ad = w.adapters["local"]
    calls = []
    report = tmp_path / "site" / "sessions" / sid / "pip-report.json"
    orig_cmd = ad.run_cmd

    def act(script, *, timeout=120.0):
        calls.append(script)
        if "--dry-run" in script:
            report.write_text(json.dumps({"install": [
                {"metadata": {"name": "newpkg", "version": "2.1"}}]}))
        for key, resp in (extra or {}).items():
            if key in script:
                return resp() if callable(resp) else resp
        return ShimResult(0, "", "")

    def cmd(script, timeout=120.0):
        calls.append(script)
        for key, resp in (extra or {}).items():
            if key in script:
                return resp() if callable(resp) else resp
        return orig_cmd(script, timeout=timeout)

    monkeypatch.setattr(ad, "run_activated", act)
    monkeypatch.setattr(ad, "run_cmd", cmd)
    return calls


def test_warm_base_pypi_only_add_is_zero_clone(tmp_path, pixi_bin,
                                               monkeypatch):
    """THE regate: the dominant field shape (pypi-only first add on a
    locally built base) no longer pays the ~10s clone it never
    needed."""
    w, sid = warm_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    calls = _pylib_fakes(monkeypatch, w, sid, tmp_path)
    out = w.session_install(sid, pypi=["newpkg"])
    assert out["mode"] == "pylib"
    assert w.store.get_session(sid)["materialize_mode"] == "pylib"
    sdir = tmp_path / "site" / "sessions" / sid
    assert not (sdir / ".pixi").exists()          # NO per-session prefix
    assert not (sdir / "pixi.toml").exists()      # clone never started
    assert not any("pixi install" in c for c in calls)
    ov = (sdir / "overlay.sh").read_text()
    assert "PYTHONPATH" in ov and "pylib" in ov
    rt = out["runtime"]
    assert rt["source"] == "base" and rt["pylib"]


def test_conda_after_pylib_refuses_with_levers(tmp_path, pixi_bin,
                                               monkeypatch):
    w, sid = warm_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    _pylib_fakes(monkeypatch, w, sid, tmp_path)
    w.session_install(sid, pypi=["newpkg"])
    out = w.session_install(sid, conda=["cmake"])
    assert out["error"] == "task.invalid"
    assert "full_clone" in out["hints"]["levers"]
    assert "snapshot" in out["hints"]["levers"]
    # session untouched: still pylib, overlay intact
    assert w.store.get_session(sid)["materialize_mode"] == "pylib"


def test_full_clone_upgrades_and_absorbs_pylib(tmp_path, pixi_bin,
                                               monkeypatch):
    w, sid = warm_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    calls = _pylib_fakes(monkeypatch, w, sid, tmp_path, extra={
        "pixi install": ShimResult(0, "cloned", ""),
        "#method": ShimResult(0, "#method uv\nreplayed", ""),
        " add --manifest-path": ShimResult(0, "", "")})
    w.session_install(sid, pypi=["newpkg"])
    out = w.session_install(sid, conda=["cmake"], full_clone=True)
    assert "error" not in out, out
    assert "absorbed" in out["upgrade_note"]
    assert w.store.get_session(sid)["materialize_mode"] == "clone"
    sdir = tmp_path / "site" / "sessions" / sid
    ov = (sdir / "overlay.sh").read_text()
    assert "pylib" not in ov                       # overlay stripped
    assert any("pixi install" in c for c in calls)      # clone happened
    assert any("#method" in c or "uv" in c for c in calls)   # replay ran
    assert any("add --manifest-path" in c and "cmake" in c
               for c in calls)                        # conda add ran


def test_upgrade_replay_failure_leaves_working_overlay(tmp_path,
                                                       pixi_bin,
                                                       monkeypatch):
    """Crash-safe ordering: replay fails => typed error, mode STILL
    pylib, overlay intact — the session keeps working; retry
    converges."""
    w, sid = warm_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    _pylib_fakes(monkeypatch, w, sid, tmp_path, extra={
        "pixi install": ShimResult(0, "cloned", ""),
        "#method": ShimResult(1, "", "uv exploded"),
        "add --pypi": ShimResult(1, "", "replay dead too")})
    w.session_install(sid, pypi=["newpkg"])
    out = w.session_install(sid, conda=["cmake"], full_clone=True)
    assert out["error"] == "env.realize_failed"
    assert "STILL WORKS" in out["detail"]
    assert w.store.get_session(sid)["materialize_mode"] == "pylib"
    ov = (tmp_path / "site" / "sessions" / sid / "overlay.sh").read_text()
    assert "pylib" in ov                           # NOT stripped


def test_cold_base_behavior_unchanged(tmp_path, pixi_bin, monkeypatch):
    """The cold shapes keep their exact contracts: pypi -> pylib,
    conda -> cold_base refusal with levers."""
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    _pylib_fakes(monkeypatch, w, sid, tmp_path)
    out = w.session_install(sid, conda=["cmake"])
    assert out["error"] == "session.cold_base"
    out2 = w.session_install(sid, pypi=["newpkg"])
    assert out2["mode"] == "pylib"


def test_cross_device_clone_says_so(tmp_path, pixi_bin, monkeypatch):
    w, sid = warm_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    _pylib_fakes(monkeypatch, w, sid, tmp_path, extra={
        "stat -c %d": ShimResult(0, "cross", ""),
        "pixi install": ShimResult(0, "cloned", ""),
        "#method": ShimResult(0, "#method uv\nok", "")})
    out = w.session_install(sid, pypi=["newpkg"], full_clone=True)
    assert "error" not in out, out
    assert "DIFFERENT devices" in out["cross_device_note"]
    kinds = [e["kind"] for e in w.store.events_since(0, 300)]
    assert "session.cross_device" in kinds


def test_same_device_clone_is_silent(tmp_path, pixi_bin, monkeypatch):
    w, sid = warm_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    _pylib_fakes(monkeypatch, w, sid, tmp_path, extra={
        "stat -c %d": ShimResult(0, "same", ""),
        "pixi install": ShimResult(0, "cloned", ""),
        "#method": ShimResult(0, "#method uv\nok", "")})
    out = w.session_install(sid, pypi=["newpkg"], full_clone=True)
    assert "error" not in out, out
    assert "cross_device_note" not in out


def test_probe_reports_reflink_capability(tmp_path):
    """The shim's probe answers reflink for the root volume — the hint
    consumers gate eager pre-warming on. Runs the REAL shim on this
    host (APFS/ext4 answer true; anything else false; only a broken
    probe says unknown)."""
    import json as _json
    import subprocess
    from pathlib import Path
    shim = Path(__file__).parent.parent.parent / "src/weft/shim/weft-shim"
    r = subprocess.run(["sh", str(shim), "probe", "--root",
                        str(tmp_path)], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    got = _json.loads(r.stdout)["storage"]["reflink"]
    assert got in (True, False, "unknown")
    assert got is not None


def test_installer_after_pylib_refuses_with_levers(tmp_path, pixi_bin,
                                                   monkeypatch):
    """The regate's sibling gap: a warm pylib session has no prefix for
    a bespoke installer to run in — same contract as conda-after-pylib
    (explicit upgrade or typed refusal), never a shell-hook against a
    manifest that does not exist."""
    w, sid = warm_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    _pylib_fakes(monkeypatch, w, sid, tmp_path)
    w.session_install(sid, pypi=["newpkg"])
    out = w.session_run_installer(sid, "./install.sh")
    assert out["error"] == "task.invalid"
    assert "full_clone" in out["hints"]["levers"]
    assert "snapshot" in out["hints"]["levers"]
    assert w.store.get_session(sid)["materialize_mode"] == "pylib"


def test_installer_full_clone_upgrades_then_runs(tmp_path, pixi_bin,
                                                 monkeypatch):
    w, sid = warm_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    calls = _pylib_fakes(monkeypatch, w, sid, tmp_path, extra={
        "pixi install": ShimResult(0, "cloned", ""),
        "#method": ShimResult(0, "#method uv\nreplayed", ""),
        "shell-hook": ShimResult(0, "installer ran", "")})
    w.session_install(sid, pypi=["newpkg"])
    out = w.session_run_installer(sid, "./install.sh", full_clone=True)
    assert "error" not in out, out
    assert "absorbed" in out["upgrade_note"]
    assert w.store.get_session(sid)["materialize_mode"] == "clone"
    assert any("shell-hook" in c and "./install.sh" in c for c in calls)
