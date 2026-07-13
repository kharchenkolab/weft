"""G1-G4: the institutional shape — admin-owned, READ-ONLY base envs.
Users adopt in place (never write/lease there), overlay their deltas over
bases they cannot modify, and get verify-and-report (not a doomed rebuild)
when an adopted base breaks."""

import subprocess

import pytest

from weft.api import Weft

pytestmark = pytest.mark.solver

SPEC = {"name": "base", "deps": {"conda": ["python =3.12", "pip"]}}


def _chmod(path, mode):
    subprocess.run(["chmod", "-R", mode, str(path)], check=True)


@pytest.fixture
def rig(tmp_path, pixi_bin):
    """An 'admin' workspace builds the base into its root; a 'user'
    workspace mounts that root read-only via ro_roots."""
    admin = Weft(tmp_path / "admin-ws", pixi_bin=pixi_bin)
    admin_root = tmp_path / "admin-root"
    admin.register_site("local", "local", {"root": str(admin_root),
                                           "pixi_source": pixi_bin})
    env = admin.env_ensure(SPEC)["env_id"]
    j = admin.runner.wait(admin.task_submit(
        {"command": "true", "env": env, "site": "local"})["job_id"], 1800)
    assert j["state"] == "DONE", j["error"]

    _chmod(admin_root / "envs", "a-w")
    user = Weft(tmp_path / "user-ws", pixi_bin=pixi_bin)
    user.register_site("local", "local", {
        "root": str(tmp_path / "user-root"), "pixi_source": pixi_bin,
        "ro_roots": [str(admin_root)]})
    yield {"admin": admin, "user": user, "env": env,
           "admin_root": admin_root, "user_root": tmp_path / "user-root"}
    _chmod(admin_root, "u+w")      # let pytest clean tmp dirs


def test_adopts_in_place_without_writing(rig):
    user, env = rig["user"], rig["env"]
    # the user's own solve of the same spec = the same EnvID
    assert user.env_ensure(SPEC)["env_id"] == env

    j = user.runner.wait(user.task_submit(
        {"command": "python -c 'import sys; print(sys.version)'",
         "env": env, "site": "local"})["job_id"], 900)
    assert j["state"] == "DONE", j["error"]

    real = user.store.get_realization(env, "local")
    assert real["read_only"] == 1
    assert real["location"].startswith(str(rig["admin_root"]))
    # nothing was built in the user's root
    assert not (rig["user_root"] / "envs"
                / env.rsplit(":", 1)[-1]).exists()
    events = [e for e in user.events_poll(0, 500, compact=False)["events"]
              if e["kind"] == "realize.adopted"]
    assert events and events[0]["via"] == "ro-root"


def test_lifecycle_refusals_and_footprint(rig):
    user, env = rig["user"], rig["env"]
    user.env_ensure(SPEC)
    user.runner.wait(user.task_submit({"command": "true", "env": env,
                                       "site": "local"})["job_id"], 900)
    ev = user.env_evict(env, "local")
    assert ev["error"] == "task.invalid"
    assert "owner_action" in ev["hints"]

    fp = user.site_footprint("local")
    mine = next(r for r in fp["realizations"] if r["env_id"] == env)
    assert mine["evictable"] is False and mine["read_only"] is True

    # gc never plans an adopted base
    user.store._write("UPDATE realizations SET updated_at=1, last_used=1 "
                      "WHERE env_id=?", (env,))
    p = user.gc_plan("local")["sites"]["local"]
    assert env not in [r["env_id"] for r in p["evictable_realizations"]]

    rep = user.env_repair(env, "local")
    assert "not touched" in rep["note"] or "were not touched" in rep["note"]


def test_overlay_over_read_only_parent_writes_nothing_there(rig, tmp_path):
    user, env = rig["user"], rig["env"]
    user.env_ensure(SPEC)
    user.runner.wait(user.task_submit({"command": "true", "env": env,
                                       "site": "local"})["job_id"], 900)

    sentinel = tmp_path / "sentinel"
    sentinel.touch()

    child = user.env_ensure({"name": "base+emcee", "extends_env": env,
                             "deps": {"pypi": ["emcee"]}})
    assert child["delta"]["layerable"] is True
    j = user.runner.wait(user.task_submit(
        {"command": "python -c 'import emcee; print(emcee.__version__)'",
         "env": child["env_id"], "site": "local"}, force=True)["job_id"],
        1800)
    assert j["state"] == "DONE", j["error"]
    real = user.store.get_realization(child["env_id"], "local")
    assert real["strategy"] == "overlay"
    assert not real.get("read_only")       # the CHILD is the user's own

    # the proof: not one byte changed under the admin root
    r = subprocess.run(["find", str(rig["admin_root"]), "-newer",
                        str(sentinel), "-type", "f"],
                       capture_output=True, text=True)
    assert r.stdout.strip() == "", f"writes into RO root:\n{r.stdout}"


def test_broken_ro_base_reports_and_falls_through(rig):
    user, env = rig["user"], rig["env"]
    user.env_ensure(SPEC)
    user.runner.wait(user.task_submit({"command": "true", "env": env,
                                       "site": "local"})["job_id"], 900)
    assert user.store.get_realization(env, "local")["read_only"] == 1

    # the admin's copy rots (a purged tool) — the user cannot fix it
    _chmod(rig["admin_root"] / "envs", "u+w")
    victim = next((rig["admin_root"] / "envs").glob(
        "*/.pixi/envs/default/bin/pip"))
    victim.unlink()
    _chmod(rig["admin_root"] / "envs", "a-w")

    j = user.runner.wait(user.task_submit(
        {"command": "python -c 'import sys'", "env": env,
         "site": "local"}, force=True)["job_id"], 1800)
    assert j["state"] == "DONE", j["error"]     # fell through, still ran

    events = [e["kind"] for e in
              user.events_poll(0, 900, compact=False)["events"]]
    assert "realize.ro_integrity_failed" in events
    real = user.store.get_realization(env, "local")
    assert not real.get("read_only")            # now a private copy
    assert real["location"].startswith("envs/")

    # WRITABLE-FIRST precedence from here on: the healthy private copy
    # wins over the (still broken) read-only one
    j2 = user.runner.wait(user.task_submit(
        {"command": "true", "env": env, "site": "local"},
        force=True)["job_id"], 900)
    assert j2["state"] == "DONE"
    assert not user.store.get_realization(env, "local").get("read_only")


def test_ro_integrity_fail_policy_stops(rig):
    user, env = rig["user"], rig["env"]
    cfg = user.store.get_site("local")["config"]
    cfg.setdefault("policy", {})["ro_integrity"] = "fail"
    user.register_site("local", "local", cfg)
    user.env_ensure(SPEC)
    user.runner.wait(user.task_submit({"command": "true", "env": env,
                                       "site": "local"})["job_id"], 900)

    _chmod(rig["admin_root"] / "envs", "u+w")
    victim = next((rig["admin_root"] / "envs").glob(
        "*/.pixi/envs/default/bin/pip"))
    victim.unlink()
    _chmod(rig["admin_root"] / "envs", "a-w")

    j = user.runner.wait(user.task_submit(
        {"command": "true", "env": env, "site": "local"},
        force=True)["job_id"], 900)
    assert j["state"] == "FAILED"
    assert "owner_action" in j["error"]["hints"]
