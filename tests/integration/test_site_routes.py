"""Round I: site-to-site data routing — shared-FS links, direct pulls,
controller detour as the honest fallback. The controller should carry
bytes only when no better path exists."""

import pytest

from weft.api import Weft

pytestmark = pytest.mark.solver


@pytest.fixture
def w(tmp_path, pixi_bin):
    """Two 'sites' that genuinely share a filesystem (one machine)."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("alpha", "local", {"root": str(tmp_path / "root-a"),
                                       "pixi_source": pixi_bin})
    w.register_site("beta", "local", {"root": str(tmp_path / "root-b"),
                                      "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.3
    return w


def _produce_at(w, site):
    env = w.env_ensure({"name": "gen",
                        "deps": {"conda": ["python =3.12"]}})["env_id"]
    j = w.runner.wait(w.task_submit({
        "command": "python -c \"open('results/field.dat','w')"
                   ".write('phi=' + '0.137'*2000)\"",
        "env": env, "outputs": ["results/"], "site": site},
        force=True)["job_id"], 1800)
    assert j["state"] == "DONE", j["error"]
    return env, next(o["ref"] for o in j["manifest"]["outputs"]
                     if o["path"] == "results/field.dat")


def test_route_probe_finds_the_shared_filesystem(w):
    r = w.store.get_route("alpha", "beta")
    assert r and r["shared_fs_path"], r
    desc = w.sites_describe("beta")
    assert any(x["via"] == "shared-fs" for x in desc["routes"])


def test_shared_fs_staging_never_moves_bytes_through_the_controller(w):
    env, ref = _produce_at(w, "alpha")
    assert w.cas.kind_of(ref) is None       # bytes live at alpha only

    task = {"command": "wc -c < d/field.dat > results/n.txt",
            "env": env, "inputs": [{"ref": ref, "mount_as": "d/field.dat"}],
            "outputs": ["results/"], "site": "beta"}
    plan = w.task_submit(task, dry_run=True)
    assert plan["plan"]["staging"]["site_to_site"][0]["via"] == "fs-link"

    j = w.runner.wait(w.task_submit(task, force=True)["job_id"], 900)
    assert j["state"] == "DONE", j["error"]
    out = next(o for o in j["manifest"]["outputs"]
               if o["path"] == "results/n.txt")
    assert "10004" in out["preview"]["lines"][0]

    # the proof of the route: the workspace CAS still lacks the bytes
    assert w.cas.kind_of(ref) is None
    events = [e for e in w.events_poll(0, 900, compact=False)["events"]
              if e["kind"] == "transfer.done"]
    assert any(e.get("via") == "fs-link" and e.get("src") == "alpha"
               for e in events)


def test_no_route_falls_back_to_controller_detour(w):
    env, ref = _produce_at(w, "alpha")
    # sever the route (simulates distinct machines, no path)
    w.store.set_route("alpha", "beta", None, False)
    task = {"command": "wc -c < d/field.dat > results/n.txt",
            "env": env, "inputs": [{"ref": ref, "mount_as": "d/field.dat"}],
            "outputs": ["results/"], "site": "beta"}
    j = w.runner.wait(w.task_submit(task, force=True)["job_id"], 900)
    assert j["state"] == "DONE", j["error"]
    # the detour is visible: bytes came HOME first, then went out
    assert w.cas.kind_of(ref) == "file"
    events = [e for e in w.events_poll(0, 900, compact=False)["events"]
              if e["kind"] == "transfer.done"]
    assert any(e.get("via") == "controller-detour" for e in events)


def test_probe_guards(w):
    r = w.site_route_probe("alpha", "alpha")
    assert r["error"] == "task.invalid"
