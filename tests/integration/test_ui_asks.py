"""Round 12: the weft-ui upstream asks (misc/from-weft-ui.md).

Enumeration tools, site_unregister, persisted submit plans,
bootstrap.step narration + probe_only registration, and the
embedder-set audit actor.
"""

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def test_enumeration_tools_shapes(w, tmp_path):
    r1 = w.task_submit({"command": "true", "site": "local"})
    assert w.runner.wait(r1["job_id"], 60)["state"] == "DONE"

    out = w.jobs_where(limit=10)
    assert out["count"] >= 1 and out["jobs"][0]["job_id"]
    assert "superseded_by" in out["jobs"][0]
    only_done = w.jobs_where(state="DONE", limit=1)
    assert only_done["count"] == 1

    assert w.list_envs() == {"envs": []}          # nothing solved yet
    assert w.list_kernels()["kernels"] == []
    assert w.list_services()["services"] == []

    trail = w.audit_tail(10)["audit"]
    assert any(a["action"] == "site.register" for a in trail)


def test_site_unregister_guardrails_and_forgetting(w, tmp_path):
    # live work refuses
    r = w.task_submit({"command": "sleep 30", "site": "local"})
    out = w.site_unregister("local")
    assert out["error"] == "state.conflict", out
    assert r["job_id"] in out["hints"]["jobs"]
    w.task_cancel(r["job_id"], why="test teardown")
    import time
    for _ in range(50):
        if w.store.get_job(r["job_id"])["state"] == "CANCELLED":
            break
        time.sleep(0.2)

    out = w.site_unregister("local")
    assert out["state"] == "unregistered"
    assert all(s["name"] != "local" for s in w.sites_list())
    assert "local" not in w.adapters
    assert w.store.routes_for("local") == []
    # unknown site is a structured refusal
    again = w.site_unregister("local")
    assert again["error"] == "task.invalid"
    # re-registering the same root re-adopts cleanly
    r2 = w.register_site("local", "local", {"root": str(tmp_path / "site")})
    assert r2["capabilities"]["os"]


def test_plan_persists_across_restart(w, tmp_path, pixi_bin):
    data = tmp_path / "ws" / "in.dat"
    data.write_bytes(b"z" * 20_000)
    ref = w.data_register("in.dat")["ref"]
    r = w.task_submit({"command": "wc -c < d/in > results/n.txt",
                       "inputs": [{"ref": ref, "mount_as": "d/in"}],
                       "outputs": ["results/"], "site": "local"})
    assert r["plan"]["staging"]["bytes_to_move"] > 0
    assert w.runner.wait(r["job_id"], 60)["state"] == "DONE"

    ra = w.task_submit({"command": "true", "array": 3, "site": "local"})

    # a fresh controller on the same workspace still knows the promises
    w2 = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    st = w2.task_status(r["job_id"])[0]
    assert st["plan"]["staging"]["bytes_to_move"] == \
        r["plan"]["staging"]["bytes_to_move"]
    astat = w2.array_status(ra["group"])
    assert astat["plan"] is not None
    # element rows ride the group plan
    el = w2.store.jobs_in_group(ra["group"])[0]
    assert w2.task_status(el["job_id"])[0]["plan"] == astat["plan"]


def test_probe_only_registers_nothing(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    out = w.register_site("candidate", "local",
                          {"root": str(tmp_path / "maybe-site")},
                          probe_only=True)
    assert out["probe_only"] is True
    assert out["capabilities"]["cpus"] >= 1
    assert w.sites_list() == []
    assert "candidate" not in w.adapters
    # the honest caveat: the shim was written to run a real probe
    assert (tmp_path / "maybe-site" / "bin" / "weft-shim").exists()


def test_bootstrap_steps_narrate_registration(w):
    steps = [e["step"] for e in
             w.events_poll(0, 500, compact=False)["events"]
             if e["kind"] == "bootstrap.step"]
    assert steps[:2] == ["bootstrap", "probe"]


def test_audit_actor_is_embedder_set(tmp_path, pixi_bin):
    wu = Weft(tmp_path / "ws-ui", pixi_bin=pixi_bin, default_actor="user")
    wu.register_site("local", "local", {"root": str(tmp_path / "site-u")})
    r = wu.task_submit({"command": "true", "site": "local"})
    assert wu.runner.wait(r["job_id"], 60)["state"] == "DONE"
    trail = wu.audit_tail(20)["audit"]
    submit = next(a for a in trail if a["action"] == "task.submit")
    assert submit["actor"] == "user"          # the embedder said so
    reg = next(a for a in trail if a["action"] == "site.register")
    assert reg["actor"] == "user"             # always user (doctrine)

    wa = Weft(tmp_path / "ws-agent", pixi_bin=pixi_bin)  # default
    wa.register_site("local", "local", {"root": str(tmp_path / "site-a")})
    r2 = wa.task_submit({"command": "true", "site": "local"})
    assert wa.runner.wait(r2["job_id"], 60)["state"] == "DONE"
    submit2 = next(a for a in wa.audit_tail(20)["audit"]
                   if a["action"] == "task.submit")
    assert submit2["actor"] == "agent"
