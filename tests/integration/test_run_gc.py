"""Retention R4 (misc/retention.md): both GC halves — sandbox discard +
TTL sweep, and explicit reclamation of the retained tier. Holdings die;
knowledge (the terminal inventory) never does."""

import json
import time
from pathlib import Path

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def _run(w, cmd):
    r = w.task_submit({"command": cmd, "site": "local"})
    assert w.runner.wait(r["job_id"], 120)["state"] == "DONE"
    return r["job_id"]


def test_discard_spares_retained_and_knowledge(w):
    jid = _run(w, "echo keep > fig.png; echo junk > scratch.tmp")
    kept = w.run_retain(jid, include=["fig.png"], background=False, dest="@workspace")
    dest = Path(kept["location"]["path"])

    out = w.run_discard(jid)
    assert out["state"] == "discarded"
    jobdir = w.adapters["local"].path(f"jobs/{jid}")
    assert not Path(jobdir).exists()                 # sandbox gone
    assert (dest / "fig.png").read_text() == "keep\n"  # holding survives
    inv = w.run_inventory(jid)                       # knowledge survives
    assert "scratch.tmp" in {e["path"] for e in inv["entries"]}

    # refusals: discarding a RUNNING run is sabotage
    r = w.task_submit({"command": "sleep 30", "site": "local"})
    time.sleep(0.8)
    bad = w.run_discard(r["job_id"])
    assert bad["error"] == "task.invalid"
    w.task_cancel(r["job_id"])


def test_ttl_defaults_off_and_sweeps_when_opted_in(w, tmp_path,
                                                   pixi_bin):
    # DEFAULT OFF (retention2): a year-old sandbox is untouchable
    # unless the site opted into the TTL
    jid = _run(w, "echo residue > r.txt")
    w.store._write("UPDATE jobs SET updated_at=? WHERE job_id=?",
                   (time.time() - 365 * 86400, jid))
    plan = w.gc_plan("local")["sites"]["local"]
    assert plan["run_remains_days_policy"] is None
    assert plan["run_remains"] == []
    w.gc_sweep("local", confirm=True)
    assert Path(w.adapters["local"].path(f"jobs/{jid}")).exists()

    # OPT-IN: the policy sweeps aged sandboxes as before
    w2 = Weft(tmp_path / "ws-opt", pixi_bin=pixi_bin)
    w2.register_site("local", "local", {
        "root": str(tmp_path / "site-opt"), "pixi_source": pixi_bin,
        "policy": {"run_remains_days": 14}})
    w2.runner.poll_interval = 0.2
    r = w2.task_submit({"command": "echo residue > r.txt",
                        "site": "local"})
    jid2 = r["job_id"]
    assert w2.runner.wait(jid2, 120)["state"] == "DONE"
    plan = w2.gc_plan("local")["sites"]["local"]
    assert jid2 not in {x["target"] for x in plan["run_remains"]}  # fresh
    w2.store._write("UPDATE jobs SET updated_at=? WHERE job_id=?",
                    (time.time() - 30 * 86400, jid2))
    plan = w2.gc_plan("local")["sites"]["local"]
    assert jid2 in {x["target"] for x in plan["run_remains"]}
    swept = w2.gc_sweep("local", confirm=True)
    assert swept["swept_run_remains"] >= 1
    assert not Path(w2.adapters["local"].path(f"jobs/{jid2}")).exists()
    # knowledge survives the sweep too
    assert w2.run_inventory(jid2)["entries"]
    ev = [e for e in w2.events_poll(0, 500)["events"]
          if e["kind"] == "run.remains_swept" and e["target"] == jid2]
    assert ev, "sweep must announce itself"


def test_forget_reclaims_by_target_and_label(w):
    j1 = _run(w, "echo a > a.txt")
    j2 = _run(w, "echo b > b.txt")
    w.run_retain(j1, include=["a.txt"], label="proj-9", background=False, dest="@workspace")
    w.run_retain(j2, include=["b.txt"], label="proj-9", background=False, dest="@workspace")
    locs = {r["target"]: Path(r["location"])
            for r in w.retained_runs(label="proj-9")}

    out = w.run_forget(label="proj-9")
    assert {f["target"] for f in out["forgotten"]} == {j1, j2}  # receipt
    for p in locs.values():
        assert not p.exists()
    assert w.retained_runs(label="proj-9") == []
    # idempotent: forgetting again is a calm no-op
    again = w.run_forget(label="proj-9")
    assert again["forgotten"] == [] and "already" in again["note"]
    # knowledge survives forget — archive-as-inventory-only, for free
    assert w.run_inventory(j1)["entries"]


@pytest.mark.docker
def test_forget_pending_on_unreachable_site(tmp_path, pixi_bin, sshd_site):
    """The index row survives an unconfirmable delete: no stale-index
    lies, retry completes it."""
    import subprocess
    w = Weft(tmp_path / "ws-f", pixi_bin=pixi_bin)
    w.register_site("beam", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin,
        "retain": {"dir": "/home/physicist/longterm"}})
    w.runner.poll_interval = 0.3
    r = w.task_submit({"command": "echo x > keep.txt", "site": "beam"})
    assert w.runner.wait(r["job_id"], 300)["state"] == "DONE"
    kept = w.run_retain(r["job_id"], include=["keep.txt"],
                        background=False)
    assert kept["state"] == "done" and kept["in_place"]

    subprocess.run(["docker", "pause", sshd_site["container"]], check=True)
    try:
        out = w.run_forget(target=r["job_id"])
        assert out["forgotten"] == []
        assert out["forget_pending"][0]["retryable"] is True
        row = w.retained_runs()[0]
        assert row["state"] == "forget_pending"      # index never lies
    finally:
        subprocess.run(["docker", "unpause", sshd_site["container"]],
                       check=True)
    out = w.run_forget(target=r["job_id"])           # retry completes
    assert out["forgotten"][0]["target"] == r["job_id"]
    assert w.retained_runs() == []


def test_forget_requires_exactly_one_selector(w):
    r = w.run_forget()
    assert r["error"] == "task.invalid"
    r = w.run_forget(target="x", label="y")
    assert r["error"] == "task.invalid"
