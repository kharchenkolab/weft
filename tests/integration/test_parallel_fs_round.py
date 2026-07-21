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


# ── the runtime contract (aba ask: no rederiving substrate internals) ──────

def test_runtime_base_prefix_strategy(tmp_path, pixi_bin):
    w, r = _lazy_session(tmp_path, pixi_bin)
    rt = r["runtime"]                       # present on start
    assert rt == w.session_runtime(r["session_id"])   # and via the verb
    assert rt["source"] == "base" and rt["env_id"] == ENV
    loc = str(tmp_path / "site" / env_dir_rel(ENV))
    assert rt["prefix"] == f"{loc}/.pixi/envs/default"
    assert rt["activation"].startswith(f". {loc}/activate.sh")
    assert rt["ns_wrap"] is False and rt["direct_exec"] is True


def test_runtime_squashfs_base_is_not_direct_exec(tmp_path, pixi_bin):
    """The foot-gun the contract exists to prevent: a squashfs base's
    prefix is MOUNT-SCOPED — callers must go through activation."""
    w = _weft(tmp_path, pixi_bin)
    _fake_env(w)
    ro = tmp_path / "ro-tree"
    loc = _lay_realization(ro, f"envs/{ENV.rsplit(':', 1)[-1]}", "SQ",
                           marker={"strategy": "squashfs"})
    w.store.set_realization(ENV, "local", "squashfs", str(loc), "ready",
                            read_only=True)
    r = w.session_start(ENV, "local")
    rt = r["runtime"]
    assert rt["source"] == "base" and rt["env_id"] == ENV
    assert rt["prefix"] == f"{loc}/mnt/.pixi/envs/default"
    assert rt["direct_exec"] is False       # regardless of ns_wrap
    assert rt["activation"].startswith(f". {loc}/activate.sh")


def test_runtime_flips_to_session_on_materialize(tmp_path, pixi_bin):
    w, r = _lazy_session(tmp_path, pixi_bin)
    sid = r["session_id"]
    # simulate the first install's clone (the real one needs a solver)
    w.store.set_session_materialized(sid)
    rt = w.session_runtime(sid)
    sdir = str(tmp_path / "site" / "sessions" / sid)
    assert rt["source"] == "session"
    assert rt["env_id"] is None             # mutated scratch: no identity
    assert rt["prefix"] == f"{sdir}/.pixi/envs/default"
    assert "shell-hook" in rt["activation"]
    assert rt["direct_exec"] is True and rt["ns_wrap"] is False


def test_runtime_query_is_not_activity(tmp_path, pixi_bin):
    """Observation must not feed session_idle_days: polling runtime
    leaves last_used untouched."""
    w, r = _lazy_session(tmp_path, pixi_bin)
    sid = r["session_id"]
    before = w.store.get_session(sid)["last_used"]
    time.sleep(0.05)
    w.session_runtime(sid)
    assert w.store.get_session(sid)["last_used"] == before
    rows = w.list_sessions("local")
    assert rows and rows[0]["runtime"]["source"] == "base"
    assert w.store.get_session(sid)["last_used"] == before


def test_runtime_unknown_or_stopped_refused(tmp_path, pixi_bin):
    w, r = _lazy_session(tmp_path, pixi_bin)
    out = w.session_runtime("ses_nonexistent")
    assert out["error"] == "task.invalid"
    w.session_stop(r["session_id"])
    out = w.session_runtime(r["session_id"])
    assert out["error"] == "task.invalid"


# ── cold-base sessions: pylib overlay, refusals, no base re-download ───────

from weft.adapters.base import ShimResult


def _cold_session(tmp_path, pixi_bin, base_pypi=("plotpkg",)):
    w = _weft(tmp_path, pixi_bin)
    w.store.put_env(ENV, "spec_" + ENV[-12:], {
        "extras": {},
        "platforms": {"any": [
            {"kind": "pypi", "name": n, "version": "1.0"}
            for n in base_pypi]},
    }, "lock: {}", "[project]", ["any"])
    rel = env_dir_rel(ENV)
    _lay_realization(tmp_path / "site", rel, "BASE")
    # read_only == adopted here == COLD package cache
    w.store.set_realization(ENV, "local", "prefix", rel, "ready",
                            read_only=True)
    r = w.session_start(ENV, "local")
    return w, r["session_id"]


def test_cold_base_conda_add_refused_with_levers(tmp_path, pixi_bin):
    w, sid = _cold_session(tmp_path, pixi_bin)
    out = w.session_install(sid, conda=["somepkg"])
    assert out["error"] == "session.cold_base"
    opts = out["hints"]["options"]
    assert "extends" in opts and "full_clone" in opts and "warm_site" in opts


def test_cold_base_installer_refused(tmp_path, pixi_bin):
    w, sid = _cold_session(tmp_path, pixi_bin)
    out = w.session_run_installer(sid, "make install")
    assert out["error"] == "session.cold_base"


def test_cold_detection_packed_strategy(tmp_path, pixi_bin):
    """Archive-unpacked bases never populated the cache either."""
    w = _weft(tmp_path, pixi_bin)
    _fake_env(w)
    rel = env_dir_rel(ENV)
    _lay_realization(tmp_path / "site", rel, "BASE")
    w.store.set_realization(ENV, "local", "packed", rel, "ready")
    sid = w.session_start(ENV, "local")["session_id"]
    out = w.session_install(sid, conda=["somepkg"])
    assert out["error"] == "session.cold_base"


def test_cold_base_pypi_goes_pylib_two_phase(tmp_path, pixi_bin, monkeypatch):
    w, sid = _cold_session(tmp_path, pixi_bin, base_pypi=("plotpkg",))
    ad = w.adapters["local"]
    calls = []
    report = tmp_path / "site" / "sessions" / sid / "pip-report.json"

    def fake_run_activated(script, *, timeout=120.0):
        calls.append(script)
        if "--dry-run" in script:
            report.write_text(json.dumps({"install": [
                {"metadata": {"name": "newpkg", "version": "2.1"}},
                {"metadata": {"name": "plotpkg", "version": "1.5"}},
            ]}))
        return ShimResult(0, "", "")

    monkeypatch.setattr(ad, "run_activated", fake_run_activated)
    out = w.session_install(sid, pypi=["newpkg"])

    # phase A resolves WITH the base visible — no --target there
    assert "--dry-run" in calls[0] and "--report" in calls[0]
    assert "activate.sh" in calls[0] and "--target" not in calls[0]
    # phase B installs ONLY the missing closure, no dep resolution —
    # uv-preferred (fast wheel path) with pip as the always-there fallback
    assert "--no-deps" in calls[1] and "--target" in calls[1]
    assert "newpkg==2.1" in calls[1] and "plotpkg==1.5" in calls[1]
    assert "command -v uv" in calls[1]
    t = out["timings"]
    assert t["resolve_s"] >= 0 and t["fetch_s"] >= 0
    assert t["total_s"] >= t["resolve_s"]

    assert out["mode"] == "pylib"
    assert out["fetched"] == ["newpkg==2.1", "plotpkg==1.5"]
    assert out["shadows_base"] == ["plotpkg"]      # base holds plotpkg
    rt = out["runtime"]
    assert rt["source"] == "base" and rt["env_id"] is None
    assert rt["direct_exec"] is False
    assert rt["pylib"].endswith(f"sessions/{sid}/pylib")
    assert "overlay.sh" in rt["activation"]
    # the composition artifact exists and prepends the layer
    ov = (tmp_path / "site" / "sessions" / sid / "overlay.sh").read_text()
    assert "PYTHONPATH" in ov and "pylib" in ov
    assert w.store.get_session(sid)["materialize_mode"] == "pylib"
    # NO clone happened — the whole point
    assert not (tmp_path / "site" / "sessions" / sid / ".pixi").exists()
    assert not (tmp_path / "site" / "sessions" / sid / "pixi.toml").exists()

    # a second install resolves against base + existing pylib layer
    w.session_install(sid, pypi=["another"])
    assert "overlay.sh" in calls[2] and "--dry-run" in calls[2]


def test_cold_base_pypi_already_satisfied(tmp_path, pixi_bin, monkeypatch):
    w, sid = _cold_session(tmp_path, pixi_bin)
    ad = w.adapters["local"]
    calls = []
    report = tmp_path / "site" / "sessions" / sid / "pip-report.json"

    def fake_run_activated(script, *, timeout=120.0):
        calls.append(script)
        if "--dry-run" in script:
            report.write_text(json.dumps({"install": []}))
        return ShimResult(0, "", "")

    monkeypatch.setattr(ad, "run_activated", fake_run_activated)
    out = w.session_install(sid, pypi=["plotpkg"])
    assert len(calls) == 1                       # no phase B
    assert out["fetched"] == []
    assert "already satisfied" in out["note"]
    assert out["timings"]["fetch_s"] == 0        # nothing was fetched


def test_cold_base_full_clone_override_routes_to_clone(tmp_path, pixi_bin,
                                                       monkeypatch):
    w, sid = _cold_session(tmp_path, pixi_bin)
    seen = {}
    monkeypatch.setattr(w.sessions, "_materialize",
                        lambda s, a: seen.setdefault("clone", True))
    monkeypatch.setattr(w.sessions, "_fast_pypi",
                        lambda s, a, p: {"method": "stub"})
    out = w.session_install(sid, pypi=["newpkg"], full_clone=True)
    assert seen.get("clone") and out["method"] == "stub"


def test_kernel_on_cold_session_gets_pylib_hook(tmp_path, pixi_bin):
    w, sid = _cold_session(tmp_path, pixi_bin)
    k = w.kernel_start("local", "python", session_id=sid)["kernel_id"]
    try:
        act = (tmp_path / "site" / "kernels" / k / "activate.sh").read_text()
        assert "WEFT_SESSION_PYLIB" in act and "overlay.sh" in act
        assert "WEFT_SESSION_PREFIX" not in act
    finally:
        w.kernel_stop(k)


def test_local_run_cmd_timeout_is_classified(tmp_path):
    ad = LocalAdapter("l", tmp_path)
    with pytest.raises(WeftError) as ei:
        ad.run_cmd("sleep 3", timeout=0.3)
    assert ei.value.code == "site.unreachable" and ei.value.retryable


@pytest.mark.solver
@pytest.mark.slow
def test_cold_base_pylib_end_to_end(tmp_path, pixi_bin):
    """The field scenario with real packages: a REAL base env marked
    adopted (cold cache), a pypi add through the pylib lane — only the
    delta is fetched, the base prefix is untouched, and the RUNNING
    kernel sees the package on its next block (forward hook)."""
    w = _weft(tmp_path, pixi_bin)
    env = w.env_ensure({"name": "cold-base",
                        "deps": {"conda": ["python =3.12", "pip"]}})
    env_id = env["env_id"]
    r0 = w.task_submit({"command": "true", "env": env_id, "site": "local"})
    assert w.runner.wait(r0["job_id"], 900)["state"] == "DONE"
    # simulate adoption: same files, but the record says read_only —
    # exactly what _adopt_from_ro_roots writes
    real = w.store.get_realization(env_id, "local")
    w.store.set_realization(env_id, "local", real["strategy"],
                            real["location"], "ready", read_only=True)

    sid = w.session_start(env_id, "local")["session_id"]
    k = w.kernel_start("local", "python", session_id=sid)["kernel_id"]
    try:
        blk = w.kernel_exec(k, "import six", timeout=60)
        assert blk["rc"] == 1                    # not there yet

        out = w.session_install(sid, pypi=["six"])
        assert out["mode"] == "pylib", out
        assert any(m.startswith("six==") for m in out["fetched"]), out
        # NO clone: the session dir holds only the layer
        sdir = tmp_path / "site" / "sessions" / sid
        assert not (sdir / ".pixi").exists()
        assert (sdir / "pylib" / "six.py").exists()

        # live-install contract holds in pylib mode
        blk = w.kernel_exec(k, "import six; print(six.__name__)",
                            timeout=60)
        assert blk["rc"] == 0 and "six" in blk["out"], blk
        # and session_exec composes the layer too
        ex = w.session_exec(sid, "python -c 'import six; print(42)'")
        assert ex["rc"] == 0 and "42" in ex["stdout"]
    finally:
        w.kernel_stop(k)
    # snapshot mints the citable extends env from the record
    snap = w.session_snapshot(sid, verify=False)
    assert snap["env_id"].startswith("env:") and snap["env_id"] != env_id


# ── the R (cran) layer: rlib composition on ANY base ───────────────────────

def _no_toolchain(monkeypatch):
    import weft.toolchain
    monkeypatch.setattr(weft.toolchain, "ensure_toolchain",
                        lambda *a, **k: None)


def test_cran_add_composes_rlib_on_frozen_base(tmp_path, pixi_bin,
                                               monkeypatch):
    w, sid = _cold_session(tmp_path, pixi_bin)
    _no_toolchain(monkeypatch)
    ad = w.adapters["local"]
    calls = []
    monkeypatch.setattr(ad, "run_activated",
                        lambda script, timeout=120.0:
                        (calls.append(script), ShimResult(0, "", ""))[1])
    out = w.session_install(sid, cran=["praise"])

    ins = calls[0]
    assert "install.packages" in ins and 'lib="' in ins
    assert f"sessions/{sid}/rlib" in ins
    assert "repos=" in ins and "R_LIBS" in ins
    assert "activate.sh" in ins                 # under base activation
    assert "MISSING" in calls[1]                # presence verification
    assert out["mode"] == "rlib"
    assert out["installed"]["cran"] == ["praise"]
    ov = (tmp_path / "site" / "sessions" / sid / "overlay.sh").read_text()
    assert "R_LIBS" in ov
    s = w.store.get_session(sid)
    assert s["added_cran"] == ["praise"]
    assert s["materialize_mode"] == "none"      # cran never flips the mode
    rt = out["runtime"]
    assert rt["rlib"].endswith(f"sessions/{sid}/rlib")
    assert rt["env_id"] is None and rt["direct_exec"] is False
    assert "overlay.sh" in rt["activation"]


def test_cran_same_mechanism_on_warm_base(tmp_path, pixi_bin, monkeypatch):
    """Orthogonality: built-here bases get the SAME rlib lane — no
    refusal, no clone, one mechanism everywhere."""
    w, r = _lazy_session(tmp_path, pixi_bin)     # warm (not read_only)
    sid = r["session_id"]
    _no_toolchain(monkeypatch)
    ad = w.adapters["local"]
    monkeypatch.setattr(ad, "run_activated",
                        lambda script, timeout=120.0: ShimResult(0, "", ""))
    out = w.session_install(sid, cran=["praise"])
    assert out["mode"] == "rlib"
    assert not (tmp_path / "site" / "sessions" / sid / ".pixi").exists()
    assert w.store.get_session(sid)["materialize_mode"] == "none"


def test_cran_and_pypi_layers_coexist(tmp_path, pixi_bin, monkeypatch):
    w, sid = _cold_session(tmp_path, pixi_bin)
    _no_toolchain(monkeypatch)
    ad = w.adapters["local"]
    report = tmp_path / "site" / "sessions" / sid / "pip-report.json"

    def fake(script, timeout=120.0):
        if "--dry-run" in script:
            report.write_text(json.dumps({"install": [
                {"metadata": {"name": "newpkg", "version": "2.1"}}]}))
        return ShimResult(0, "", "")

    monkeypatch.setattr(ad, "run_activated", fake)
    out = w.session_install(sid, pypi=["newpkg"], cran=["praise"])
    assert out["installed"]["cran"] == ["praise"]
    assert out["fetched"] == ["newpkg==2.1"]
    ov = (tmp_path / "site" / "sessions" / sid / "overlay.sh").read_text()
    assert "PYTHONPATH" in ov and "R_LIBS" in ov
    rt = w.session_runtime(sid)
    assert rt["pylib"] and rt["rlib"]


def test_cran_verification_catches_silent_r_failure(tmp_path, pixi_bin,
                                                    monkeypatch):
    """install.packages exits 0 on failure — the presence check must
    turn that into an honest error."""
    w, sid = _cold_session(tmp_path, pixi_bin)
    _no_toolchain(monkeypatch)
    ad = w.adapters["local"]
    monkeypatch.setattr(
        ad, "run_activated",
        lambda script, timeout=120.0:
        ShimResult(1 if "MISSING" in script else 0,
                   "MISSING: praise", ""))
    out = w.session_install(sid, cran=["praise"])
    assert out["error"] == "env.solve_conflict"
    assert w.store.get_session(sid)["added_cran"] == []   # not recorded


def test_kernel_session_gets_rlib_hook(tmp_path, pixi_bin):
    w, sid = _cold_session(tmp_path, pixi_bin)
    k = w.kernel_start("local", "python", session_id=sid)["kernel_id"]
    try:
        act = (tmp_path / "site" / "kernels" / k / "activate.sh").read_text()
        assert "WEFT_SESSION_RLIB" in act and "overlay.sh" in act
    finally:
        w.kernel_stop(k)


def test_snapshot_carries_cran_layer(tmp_path, pixi_bin, monkeypatch):
    w, sid = _cold_session(tmp_path, pixi_bin)
    w.store.session_add_deps(sid, [], [], ["praise", "jsonlite ==2.0.1"])
    seen = {}

    def fake_ensure(spec, **kw):
        seen["spec"] = spec
        return {"env_id": "env:v1:" + "f" * 12}

    monkeypatch.setattr(w.sessions.envman, "ensure", fake_ensure)
    snap = w.session_snapshot(sid, verify=False)
    assert seen["spec"]["deps"]["cran"] == ["praise", "jsonlite ==2.0.1"]
    assert snap["env_id"] == "env:v1:" + "f" * 12
    # and a cran-only session does NOT short-circuit to the base
    assert snap["env_id"] != ENV


def test_cold_refusal_names_the_delta_lanes(tmp_path, pixi_bin):
    w, sid = _cold_session(tmp_path, pixi_bin)
    out = w.session_install(sid, conda=["somepkg"])
    lanes = out["hints"]["delta_lanes"]
    assert "pypi" in lanes and "cran" in lanes and "conda" in lanes
    assert "rlib" in lanes["cran"]


@pytest.mark.solver
@pytest.mark.slow
def test_cran_rlib_end_to_end_on_adopted_base(tmp_path, pixi_bin):
    """The R frozen-base scenario with real packages: adopted R base,
    cran add via rlib — delta-only, base untouched, and the RUNNING R
    kernel sees the package on its next library() call (driver.R hook)."""
    w = _weft(tmp_path, pixi_bin)
    env = w.env_ensure({"name": "r-frozen",
                        "deps": {"conda": ["r-base =4.4"]}})
    env_id = env["env_id"]
    r0 = w.task_submit({"command": "true", "env": env_id, "site": "local"})
    assert w.runner.wait(r0["job_id"], 900)["state"] == "DONE"
    real = w.store.get_realization(env_id, "local")
    w.store.set_realization(env_id, "local", real["strategy"],
                            real["location"], "ready", read_only=True)

    sid = w.session_start(env_id, "local")["session_id"]
    k = w.kernel_start("local", "r", session_id=sid)["kernel_id"]
    try:
        blk = w.kernel_exec(k, "library(praise)", timeout=120)
        assert blk["rc"] == 1                    # not there yet

        out = w.session_install(sid, cran=["praise"])
        assert out["mode"] == "rlib", out
        sdir = tmp_path / "site" / "sessions" / sid
        assert (sdir / "rlib" / "crayon").is_dir()
        assert not (sdir / ".pixi").exists()     # no clone

        # live-install contract for R: next block, no restart
        blk = w.kernel_exec(
            k, "library(praise)\ncat(class(praise()))", timeout=120)
        assert blk["rc"] == 0 and "character" in blk["out"], blk
        # session_exec composes the layer too
        ex = w.session_exec(
            sid, "Rscript -e 'library(praise); cat(\"OK\")'")
        assert ex["rc"] == 0 and "OK" in ex["stdout"]
    finally:
        w.kernel_stop(k)
    snap = w.session_snapshot(sid, verify=False)
    assert snap["env_id"].startswith("env:") and snap["env_id"] != env_id



# ── perf plumbing: pip/uv caches are a property of the SITE ROOT ───────────

def test_pip_uv_caches_ride_the_site_root(tmp_path):
    from weft.adapters.ssh import SSHAdapter
    la = LocalAdapter("l", tmp_path)
    env = la._env()
    assert env["PIP_CACHE_DIR"] == f"{tmp_path}/cache/pip"
    assert env["UV_CACHE_DIR"] == f"{tmp_path}/cache/uv"
    sa = SSHAdapter("s", "host", "/site/root")
    pre = sa._env_prefix()
    assert "PIP_CACHE_DIR=/site/root/cache/pip" in pre
    assert "UV_CACHE_DIR=/site/root/cache/uv" in pre


# ── exec_template: the thing out-of-band consumers CAN exec ────────────────

import shlex as _shlex
import subprocess as _subprocess


def test_exec_template_runs_argv_in_base_env(tmp_path, pixi_bin):
    w, r = _lazy_session(tmp_path, pixi_bin)
    t = r["runtime"]["exec_template"]
    # the consumer contract: shlex.split(template) + argv, exec'd on site
    out = _subprocess.run(_shlex.split(t) + ["weft-probe"],
                          capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "BASE", out


def test_exec_template_composes_session_layers(tmp_path, pixi_bin,
                                               monkeypatch):
    w, sid = _cold_session(tmp_path, pixi_bin)
    _no_toolchain(monkeypatch)
    ad = w.adapters["local"]
    report = tmp_path / "site" / "sessions" / sid / "pip-report.json"

    def fake(script, timeout=120.0):
        if "--dry-run" in script:
            report.write_text(json.dumps({"install": [
                {"metadata": {"name": "newpkg", "version": "1.0"}}]}))
        return ShimResult(0, "", "")

    monkeypatch.setattr(ad, "run_activated", fake)
    w.session_install(sid, pypi=["newpkg"])
    t = w.session_runtime(sid)["exec_template"]
    out = _subprocess.run(
        _shlex.split(t) + ["sh", "-c", "echo $PYTHONPATH"],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0
    assert f"sessions/{sid}/pylib" in out.stdout


def test_exec_template_carries_ns_layer(tmp_path, pixi_bin, monkeypatch):
    w, r = _lazy_session(tmp_path, pixi_bin)
    monkeypatch.setattr(w.runner, "ns_wrap_needed", lambda *a: True)
    t = w.session_runtime(r["session_id"])["exec_template"]
    assert t.startswith("unshare -rm ")
    # and the clone-mode template composes shell-hook activation
    w.store.set_session_materialized(r["session_id"])
    t2 = w.session_runtime(r["session_id"])["exec_template"]
    assert "shell-hook" in t2 and 'exec "$@"' in t2
    assert not t2.startswith("unshare")        # clone: plain prefix


# ── mountpoint tombstones: bare execs die legibly ──────────────────────────

def test_tombstones_write_exec_and_strip(tmp_path):
    from weft.realize import (_strip_mount_tombstones,
                              _write_mount_tombstones)
    ad = LocalAdapter("l", tmp_path)
    _write_mount_tombstones(ad, "envs/e1")
    shim = tmp_path / "envs/e1/mnt/.pixi/envs/default/bin/python"
    assert shim.exists()
    r = _subprocess.run([str(shim)], capture_output=True, text=True)
    assert r.returncode == 127
    assert "exec_template" in r.stderr and "namespace" in r.stderr
    for name in ("python3", "R", "Rscript", "julia"):
        assert (shim.parent / name).exists()
    _strip_mount_tombstones(ad, "envs/e1")
    assert not (tmp_path / "envs/e1/mnt/.pixi").exists()
    assert (tmp_path / "envs/e1/mnt").exists()   # the mountpoint stays


# ── non-CRAN R: one vocabulary (github refs, extra repos), writes_to ───────

def test_cran_github_ref_routes_via_remotes(tmp_path, pixi_bin,
                                            monkeypatch):
    w, sid = _cold_session(tmp_path, pixi_bin)
    _no_toolchain(monkeypatch)
    ad = w.adapters["local"]
    calls = []
    monkeypatch.setattr(ad, "run_activated",
                        lambda script, timeout=120.0:
                        (calls.append(script), ShimResult(0, "", ""))[1])
    out = w.session_install(sid, cran=["org/widgetlib@v2.1"])
    assert len(calls) == 1                      # refs: no dir-verify pass
    ins = calls[0]
    assert 'remotes::install_github("org/widgetlib@v2.1"' in ins
    assert f'lib="{tmp_path}/site/sessions/{sid}/rlib"' in ins
    assert 'requireNamespace("remotes"' in ins  # self-bootstrap guard
    assert "install.packages(c()" not in ins    # no empty plain install
    assert out["installed"]["cran"] == ["org/widgetlib@v2.1"]
    # the RECORD is the spec string — the snapshot's solve SHA-pins it
    assert w.store.get_session(sid)["added_cran"] == ["org/widgetlib@v2.1"]


def test_cran_mixed_refs_repos_and_snapshot(tmp_path, pixi_bin,
                                            monkeypatch):
    w, sid = _cold_session(tmp_path, pixi_bin)
    _no_toolchain(monkeypatch)
    ad = w.adapters["local"]
    calls = []
    monkeypatch.setattr(ad, "run_activated",
                        lambda script, timeout=120.0:
                        (calls.append(script), ShimResult(0, "", ""))[1])
    w.session_install(sid, cran=["toolpkg", "org/widgetlib@v2"],
                      cran_repos=["https://repo.example.org/cranlike"])
    ins = calls[0]
    assert '"https://repo.example.org/cranlike"' in ins   # extra repo FIRST
    assert 'install.packages(c("toolpkg")' in ins
    assert 'remotes::install_github("org/widgetlib@v2"' in ins
    s = w.store.get_session(sid)
    assert s["added_cran_repos"] == ["https://repo.example.org/cranlike"]

    seen = {}
    monkeypatch.setattr(w.sessions.envman, "ensure",
                        lambda spec, **kw: (seen.update(spec=spec)
                                            or {"env_id": "env:v1:" + "e" * 12}))
    w.session_snapshot(sid, verify=False)
    assert seen["spec"]["deps"]["cran"] == ["toolpkg", "org/widgetlib@v2"]
    assert seen["spec"]["r_repositories"] == \
        ["https://repo.example.org/cranlike"]


def test_installer_undeclared_refused_declared_runs(tmp_path, pixi_bin,
                                                    monkeypatch):
    w, sid = _cold_session(tmp_path, pixi_bin)
    out = w.session_run_installer(sid, "Rscript -e 'somepkg::install()'")
    assert out["error"] == "session.cold_base"
    assert "writes_to" in out["hints"]["delta_lanes"]["installer"]
    out = w.session_run_installer(sid, "true", writes_to="attic")
    assert out["error"] == "session.cold_base"   # only rlib|pylib declared

    ad = w.adapters["local"]
    calls = []
    monkeypatch.setattr(ad, "run_activated",
                        lambda script, timeout=120.0:
                        (calls.append(script), ShimResult(0, "", ""))[1])
    got = w.session_run_installer(
        sid, "Rscript -e 'remotes::install_local(\"x\")'",
        writes_to="rlib", note="vendored widget lib")
    ins = calls[0]
    sdir = f"{tmp_path}/site/sessions/{sid}"
    assert f"mkdir -p {sdir}/rlib" in ins
    assert "R_LIBS=" in ins and "activate.sh" in ins
    assert got["writes_to"] == "rlib"
    assert "post_install" in got["note"]        # the overlay-cost honesty
    ov = (tmp_path / "site" / "sessions" / sid / "overlay.sh").read_text()
    assert "R_LIBS" in ov
    # recorded: the snapshot will replay it as a post_install step
    assert w.store.get_session(sid)["installers"]


def test_installer_writes_to_pylib_points_pip(tmp_path, pixi_bin,
                                              monkeypatch):
    w, sid = _cold_session(tmp_path, pixi_bin)
    ad = w.adapters["local"]
    calls = []
    monkeypatch.setattr(ad, "run_activated",
                        lambda script, timeout=120.0:
                        (calls.append(script), ShimResult(0, "", ""))[1])
    got = w.session_run_installer(sid, "pip install ./vendored",
                                  writes_to="pylib")
    ins = calls[0]
    assert "PIP_TARGET=" in ins and "pylib" in ins
    assert got["writes_to"] == "pylib"


@pytest.mark.solver
@pytest.mark.slow
def test_cran_github_ref_end_to_end(tmp_path, pixi_bin):
    """A REAL github R package onto an adopted base: remotes bootstraps
    itself into the rlib, the ref installs delta-only, the running R
    kernel sees it live, and the snapshot records the spec string."""
    w = _weft(tmp_path, pixi_bin)
    env = w.env_ensure({"name": "r-frozen-gh",
                        "deps": {"conda": ["r-base =4.4"]}})
    env_id = env["env_id"]
    r0 = w.task_submit({"command": "true", "env": env_id, "site": "local"})
    assert w.runner.wait(r0["job_id"], 900)["state"] == "DONE"
    real = w.store.get_realization(env_id, "local")
    w.store.set_realization(env_id, "local", real["strategy"],
                            real["location"], "ready", read_only=True)

    sid = w.session_start(env_id, "local")["session_id"]
    out = w.session_install(sid, cran=["r-lib/crayon"])
    assert out["mode"] == "rlib", out
    sdir = tmp_path / "site" / "sessions" / sid
    assert (sdir / "rlib" / "crayon").is_dir()
    assert (sdir / "rlib" / "remotes").is_dir()   # self-bootstrapped
    assert not (sdir / ".pixi").exists()          # still no clone

    k = w.kernel_start("local", "r", session_id=sid)["kernel_id"]
    try:
        blk = w.kernel_exec(
            k, 'library(crayon)\ncat(class(crayon::green("ok")))', timeout=120)
        assert blk["rc"] == 0 and "character" in blk["out"], blk
    finally:
        w.kernel_stop(k)
    assert w.store.get_session(sid)["added_cran"] == ["r-lib/crayon"]
