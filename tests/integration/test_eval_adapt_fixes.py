"""Regressions for the adaptivity live-agent eval findings."""

import pytest

from weft.api import Weft

pytestmark = [pytest.mark.solver, pytest.mark.slow]


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def _make_pkg(root):
    pkg = root / "mypkg"
    (pkg / "mymod").mkdir(parents=True)
    (pkg / "mymod" / "__init__.py").write_text(
        'def tag():\n    return "portable"\n')
    (pkg / "pyproject.toml").write_text(
        '[project]\nname = "mymod"\nversion = "0.1"\n')
    return pkg


def test_portable_escape_hatch_rebuilds_from_scratch(w, tmp_path):
    """THE landmine the live agent found: a session installer that reads a
    local path used to mint a 'citable' EnvID that could never be rebuilt.
    With source=, the sources are content-addressed and travel with the env."""
    pkg = _make_pkg(tmp_path / "ws")
    s = w.session_start({"name": "hatch",
                         "deps": {"conda": ["python =3.12", "pip"]}}, "local")
    sid = s["session_id"]
    r = w.session_run_installer(sid, "pip install ./mypkg",
                                note="local-only helper; no index has it",
                                source=str(pkg))
    assert r["portable"] is True and r["source_ref"].startswith("dref:")

    snap = w.session_snapshot(sid, notes=["vendored until published"])
    assert "env_id" in snap, snap
    assert snap.get("verified") is True          # it was REALIZED, not just solved
    assert snap["spec"]["post_install_inputs"]
    w.session_stop(sid)

    # the proof: delete the source, wipe the realization, rebuild elsewhere
    import shutil
    shutil.rmtree(pkg)
    w.env_repair(snap["env_id"], "local")
    j = w.runner.wait(w.task_submit({
        "command": "python -c 'import mymod; print(mymod.tag())' "
                   "> results/o.txt",
        "env": snap["env_id"], "outputs": ["results/"],
        "site": "local"}, force=True)["job_id"], 1800)
    assert j["state"] == "DONE", j["error"]
    out = next(o for o in j["manifest"]["outputs"]
               if o["path"] == "results/o.txt")
    assert out["preview"]["lines"] == ["portable"]

    # and the grade tells a reader it rebuilds anywhere
    comps = w.env_status(snap["env_id"])["summary"]["reproducibility_components"]
    pi = next(c for c in comps if c["component"] == "post_install")
    assert pi["portable"] is True


def test_unportable_installer_is_flagged_at_snapshot(w, tmp_path):
    """Without source=, an installer reading a local path mints an env that
    rebuilds HERE and nowhere else. Verification can't see that (it succeeds
    locally), so weft lints for it and says so plainly — and the grade's
    portable flag agrees."""
    pkg = _make_pkg(tmp_path / "ws")
    s = w.session_start({"name": "trap",
                         "deps": {"conda": ["python =3.12", "pip"]}}, "local")
    sid = s["session_id"]
    w.session_run_installer(sid, f"pip install {pkg}",   # absolute local path
                            note="forgot to capture the source")
    snap = w.session_snapshot(sid)
    assert "env_id" in snap, snap
    warn = snap["portability_warning"]
    assert str(pkg) in warn["paths"]
    assert "rebuild on this machine and fail elsewhere" in warn["detail"]
    assert "source=<path>" in warn["fix"]

    comps = w.env_status(snap["env_id"])["summary"]["reproducibility_components"]
    pi = next(c for c in comps if c["component"] == "post_install")
    assert pi["portable"] is False
    assert "may not rebuild elsewhere" in pi["why"]

    # and the failure, when it comes, is honest: delete the source, rebuild
    import shutil
    shutil.rmtree(pkg)
    w.env_repair(snap["env_id"], "local")
    j = w.runner.wait(w.task_submit({"command": "true", "env": snap["env_id"],
                                     "site": "local"}, force=True)["job_id"],
                      900)
    assert j["state"] == "FAILED"
    assert j["error"]["error"] == "env.realize_failed"
    w.session_stop(sid)


def test_solve_conflict_names_the_soft_lever(w):
    r = w.env_ensure({"name": "c", "deps": {"conda": ["xz ==4.999.9"]}})
    assert r["error"] == "env.solve_conflict"
    assert 'relax="soft"' in r["hints"]["suggestion"]


def test_footprint_is_honest_and_orphans_reclaimable(w):
    env = w.env_ensure({"name": "fp", "deps": {"conda": ["xz >=5"]}})["env_id"]
    w.runner.wait(w.task_submit({"command": "true", "env": env,
                                 "site": "local"})["job_id"], 900)
    # freshly-written dirs get a concurrency grace window by default —
    # zero it so the just-created ghost is visible to this test
    site_cfg = w.store.get_site("local")["config"]
    site_cfg.setdefault("policy", {})["orphan_grace_minutes"] = 0
    w.register_site("local", "local", site_cfg)
    fp = w.site_footprint("local")
    assert fp["free_bytes"] > 0                     # the premise, reported
    assert fp["realizations"][0]["evictable"] is True

    # a crashed session leaves a clone behind; nothing else could reclaim it
    (w.workspace.parent / "site" / "sessions" / "ses_ghost").mkdir(
        parents=True, exist_ok=True)
    (w.workspace.parent / "site" / "sessions" / "ses_ghost" / "junk").write_text(
        "x" * 5000)
    orph = w.gc_orphans("local")
    assert any(o["name"] == "ses_ghost" for o in orph["orphans"])
    done = w.gc_orphans("local", confirm=True)
    assert done["removed"] >= 1

    ev = w.env_evict(env, "local")
    # freed_bytes is measured, not the apparent (hardlink-inflated) size
    assert ev["freed_bytes"] <= ev["apparent_bytes"]
