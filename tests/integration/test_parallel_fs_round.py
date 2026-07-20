"""Parallel-FS round (cbe BeeGFS field report): wipe-aside instead of
synchronous rm in realize's critical path, RO-pack adoption over a
broken writable copy, and LAZY session clone — a no-additions session
attaches to the base realization in place instead of laying down a
~10^5-file per-session prefix that defeats the mount it shadows."""

import json
import time
from pathlib import Path

import pytest

from weft.api import Weft
from weft.adapters.local import LocalAdapter
from weft.errors import WeftError
from weft.realize import _wipe_aside, ensure_realization, env_dir_rel


# ── wipe-aside mechanics ───────────────────────────────────────────────────

def _wait_gone(path: Path, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not path.exists():
            return True
        time.sleep(0.2)
    return False


def test_wipe_aside_is_rename_plus_background_unlink(tmp_path):
    ad = LocalAdapter("l", tmp_path)
    d = tmp_path / "envs" / "abc123"
    (d / "bin").mkdir(parents=True)
    for i in range(50):
        (d / "bin" / f"f{i}").write_text("x")
    trash = _wipe_aside(ad, "envs/abc123")
    # the dir is back, EMPTY, immediately — no O(files) wait
    assert d.is_dir() and list(d.iterdir()) == []
    assert trash and trash.startswith("envs/abc123.trash-")
    # the old tree unlinks in the background
    assert _wait_gone(tmp_path / trash)


def test_wipe_aside_recreate_false_and_clean_dir(tmp_path):
    ad = LocalAdapter("l", tmp_path)
    d = tmp_path / "envs" / "x"
    d.mkdir(parents=True)
    assert _wipe_aside(ad, "envs/x", recreate=False) is not None
    assert not d.exists()
    # nothing there -> no trash, no error
    assert _wipe_aside(ad, "envs/never-existed", recreate=False) is None


def test_wipe_aside_failure_is_structured(tmp_path):
    ad = LocalAdapter("l", tmp_path)
    d = tmp_path / "envs" / "locked"
    d.mkdir(parents=True)
    (tmp_path / "envs").chmod(0o500)   # parent read-only: rename must fail
    try:
        with pytest.raises(WeftError) as ei:
            _wipe_aside(ad, "envs/locked")
        assert ei.value.hints.get("op") == "wipe-aside"
        assert ei.value.retryable
    finally:
        (tmp_path / "envs").chmod(0o755)


# ── fabricated realizations (no solver, no network) ────────────────────────

ENV = "env:v1:deadbeefcafe"


def _weft(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def _fake_env(w, env_id=ENV):
    w.store.put_env(env_id, "spec_" + env_id[-12:], {"extras": {}},
                    "lock: {}", "[project]", ["any"])


def _lay_realization(root: Path, rel: str, probe_says: str,
                     marker: dict | None = None):
    d = root / rel
    (d / "bin").mkdir(parents=True)
    p = d / "bin" / "weft-probe"
    p.write_text(f"#!/bin/sh\necho {probe_says}\n")
    p.chmod(0o755)
    (d / "activate.sh").write_text(
        f'export PATH="{d}/bin:$PATH"\n')
    (d / ".weft-ready").write_text(
        json.dumps(marker or {"strategy": "prefix"}))
    return d


def _site_ctx(w):
    row = w.store.get_site("local") or {}
    return row.get("capabilities") or {}, row.get("config") or {}


# ── RO adoption over a BROKEN writable copy ────────────────────────────────

def test_adopt_ro_over_stale_writable(tmp_path, pixi_bin):
    w = _weft(tmp_path, pixi_bin)
    _fake_env(w)
    site_root = tmp_path / "site"
    rel = env_dir_rel(ENV)

    # a writable copy that FAILS its integrity fence (recorded digest
    # can't match) — the stale carcass of the field report
    _lay_realization(site_root, rel, "STALE",
                     marker={"strategy": "prefix", "bin_digest": "0" * 64})
    w.store.set_realization(ENV, "local", "prefix", rel, "ready")

    # an intact admin-owned copy in a read-only root
    ro = tmp_path / "ro-tree"
    _lay_realization(ro, f"envs/{ENV.rsplit(':', 1)[-1]}", "RO")
    caps, cfg = _site_ctx(w)
    cfg = {**cfg, "ro_roots": [str(ro)]}

    env_row = w.store.get_env(ENV)
    got = ensure_realization(ENV, env_row, w.adapters["local"], w.store,
                             caps=caps, site_config=cfg)
    assert got["read_only"] and got["location"].startswith(str(ro))
    assert got["state"] == "ready"
    kinds = [(e["kind"], e.get("via")) for e in
             w.events_poll(0, 500)["events"] if e["kind"] == "realize.adopted"]
    assert ("realize.adopted", "ro-over-stale") in kinds
    # the carcass is displaced (renamed aside; background unlink follows)
    assert not (site_root / rel).exists() or not any(
        (site_root / rel).iterdir())


def test_ready_intact_writable_still_wins(tmp_path, pixi_bin):
    """The writable-first guard the precedence change must NOT break: a
    deliberate healthy rebuild beats any RO copy."""
    w = _weft(tmp_path, pixi_bin)
    _fake_env(w)
    site_root = tmp_path / "site"
    rel = env_dir_rel(ENV)
    _lay_realization(site_root, rel, "MINE")   # no bin_digest: fence passes
    w.store.set_realization(ENV, "local", "prefix", rel, "ready")
    ro = tmp_path / "ro-tree"
    _lay_realization(ro, f"envs/{ENV.rsplit(':', 1)[-1]}", "RO")
    caps, cfg = _site_ctx(w)
    cfg = {**cfg, "ro_roots": [str(ro)]}
    got = ensure_realization(ENV, w.store.get_env(ENV), w.adapters["local"],
                             w.store, caps=caps, site_config=cfg)
    assert not got.get("read_only")
    assert got["location"] == rel


# ── lazy session clone ─────────────────────────────────────────────────────

def _lazy_session(tmp_path, pixi_bin):
    w = _weft(tmp_path, pixi_bin)
    _fake_env(w)
    rel = env_dir_rel(ENV)
    _lay_realization(tmp_path / "site", rel, "BASE")
    w.store.set_realization(ENV, "local", "prefix", rel, "ready")
    r = w.session_start(ENV, "local")
    return w, r


def test_session_start_is_lazy_and_runs_from_base(tmp_path, pixi_bin):
    w, r = _lazy_session(tmp_path, pixi_bin)
    sid = r["session_id"]
    assert r["materialized"] is False
    # NO per-session prefix was laid down — the whole point on BeeGFS
    sdir = tmp_path / "site" / "sessions" / sid
    assert sdir.is_dir()
    assert not (sdir / "pixi.toml").exists()
    assert not (sdir / ".pixi").exists()
    # execution attaches to the base realization in place
    out = w.session_exec(sid, "weft-probe")
    assert out["rc"] == 0 and out["stdout"].strip() == "BASE"
    assert w.store.get_session(sid)["materialized"] is False


def test_session_start_honors_recorded_ro_location(tmp_path, pixi_bin):
    """The session.py:60 bug from the assessment: readiness must be
    checked at the RECORDED location, not env_dir_rel — an adopted RO
    pack lives outside the writable root."""
    w = _weft(tmp_path, pixi_bin)
    _fake_env(w)
    ro = tmp_path / "ro-tree"
    loc = _lay_realization(ro, f"envs/{ENV.rsplit(':', 1)[-1]}", "ROBASE")
    w.store.set_realization(ENV, "local", "prefix", str(loc), "ready",
                            read_only=True)
    r = w.session_start(ENV, "local")   # must NOT try to realize/build
    out = w.session_exec(r["session_id"], "weft-probe")
    assert out["rc"] == 0 and out["stdout"].strip() == "ROBASE"


def test_kernel_on_lazy_session_runs_from_base(tmp_path, pixi_bin):
    w, r = _lazy_session(tmp_path, pixi_bin)
    sid = r["session_id"]
    k = w.kernel_start("local", "python", session_id=sid)["kernel_id"]
    try:
        blk = w.kernel_exec(
            k, "import subprocess\n"
               "print(subprocess.run(['weft-probe'], capture_output=True,"
               " text=True).stdout.strip())", timeout=120)
        assert blk["rc"] == 0 and blk["out"].strip() == "BASE"
    finally:
        w.kernel_stop(k)
    assert w.store.get_session(sid)["materialized"] is False


def test_snapshot_of_unmutated_session_is_the_base(tmp_path, pixi_bin):
    w, r = _lazy_session(tmp_path, pixi_bin)
    snap = w.session_snapshot(r["session_id"])
    assert snap["env_id"] == ENV
    assert "base env is the snapshot" in snap["note"]


def test_stop_unmaterialized_session_clean(tmp_path, pixi_bin):
    w, r = _lazy_session(tmp_path, pixi_bin)
    sid = r["session_id"]
    out = w.session_stop(sid)
    assert out["state"] == "stopped"
    assert _wait_gone(tmp_path / "site" / "sessions" / sid)


# ── gc reaps orphaned trash ────────────────────────────────────────────────

def test_gc_sweep_reaps_orphan_trash(tmp_path, pixi_bin):
    w = _weft(tmp_path, pixi_bin)
    orphan = tmp_path / "site" / "envs" / "old.trash-deadbeef"
    (orphan / "sub").mkdir(parents=True)
    (orphan / "sub" / "f").write_text("x")
    w.gc_sweep("local", confirm=True)
    assert not orphan.exists()
    kinds = [e["kind"] for e in w.events_poll(0, 500)["events"]]
    assert "gc.trash_reaped" in kinds
