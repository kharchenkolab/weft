"""Embedder-truth round (aba sole-authority note, 2026-08-09).

Two field bugs, both reproduced live before the fix:

* bug3 — a controller that dies mid-job leaves the row RUNNING forever
  while the finished task's exit record sits on disk; nothing invoked
  the (correct, complete) recovery machinery. Fix: resume= at
  construction.
* transport verdicts — site.unreachable raised inside _drive (lost
  submit reply) or inside a poller transition (walltime cancel over
  dead transport) was minted into per-job FAILED, violating the
  poller's own doctrine ("a transport failure is ONE site-level
  outage... jobs untouched"). Fix: park + jobdir-truth probe; per-item
  unreachable never reaches _fail.
"""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from weft.api import Weft
from weft.errors import WeftError

SRC = str(Path(__file__).resolve().parents[2] / "src")


def _mkweft(base: Path, pixi_bin: str, resume: str = "poll",
            policy: dict | None = None) -> Weft:
    w = Weft(base / "ws", pixi_bin=pixi_bin, resume=resume)
    if "local" not in {s["name"] for s in w.store.list_sites()}:
        cfg = {"root": str(base / "site"), "pixi_source": pixi_bin}
        if policy:
            cfg["policy"] = policy
        w.register_site("local", "local", cfg)
    w.runner.poll_interval = 0.2
    return w


def _events(w, kind=None):
    evs = w.store.events_since(0, 1000)
    return [e for e in evs if kind is None or e["kind"] == kind]


def _wait_state(w, jid, states=("DONE", "FAILED", "CANCELLED"),
                timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = w.store.get_job(jid)
        if row and row["state"] in states:
            return row
        time.sleep(0.1)
    raise AssertionError(
        f"job {jid} not in {states} after {timeout}s: "
        f"{(w.store.get_job(jid) or {}).get('state')}")


def _orphan_job(base: Path, pixi_bin: str, command: str,
                kill_after: float = 1.5) -> str:
    """Submit via a CHILD process and SIGKILL it mid-job (bug3's shape:
    the task survives its controller — detached — and finishes on disk)."""
    jid_file = base / "job_id.txt"
    child_code = f"""
import sys, time
sys.path.insert(0, {SRC!r})
from weft.api import Weft
w = Weft({str(base / 'ws')!r}, pixi_bin={pixi_bin!r}, resume="off")
if "local" not in {{s["name"] for s in w.store.list_sites()}}:
    w.register_site("local", "local", {{"root": {str(base / 'site')!r},
                                        "pixi_source": {pixi_bin!r}}})
w.runner.poll_interval = 0.2
r = w.task_submit({{"command": {command!r},
                    "outputs": ["out.txt"], "site": "local"}})
open({str(jid_file)!r}, "w").write(r["job_id"])
time.sleep(600)
"""
    child = subprocess.Popen([sys.executable, "-c", child_code])
    deadline = time.time() + 120
    while not jid_file.exists() and time.time() < deadline:
        time.sleep(0.2)
    assert jid_file.exists(), "child never submitted"
    time.sleep(kill_after)
    os.kill(child.pid, signal.SIGKILL)
    child.wait()
    return jid_file.read_text().strip()


# -- bug3: auto-resume at construction ----------------------------------------

def test_controller_death_frozen_without_resume_healed_with(tmp_path, pixi_bin):
    jid = _orphan_job(tmp_path, pixi_bin,
                      "sleep 2 && echo done > out.txt")
    jobdir = tmp_path / "site" / "jobs" / jid
    deadline = time.time() + 60
    while not (jobdir / "exit_code").exists() and time.time() < deadline:
        time.sleep(0.2)
    assert (jobdir / "exit_code").exists(), "detached task never finished"

    # resume="off" pins the OLD behavior — the lie aba filed as bug3:
    # disk says exited rc=0, the row says RUNNING, forever
    w_off = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    w_off.runner.poll_interval = 0.2
    time.sleep(1.5)
    assert w_off.store.get_job(jid)["state"] == "RUNNING"

    # resume="poll" (default) heals at CONSTRUCTION — no reconcile call
    w = _mkweft(tmp_path, pixi_bin, resume="poll")
    row = _wait_state(w, jid, ("DONE",))
    assert row["manifest"], "healed without a manifest"
    assert any(o["path"] == "out.txt" for o in row["manifest"]["outputs"])


def test_driver_lost_stamp_under_poll_then_full_completes(tmp_path, pixi_bin):
    w1 = _mkweft(tmp_path, pixi_bin, resume="off")
    w1.runner._drive = lambda job_id: None      # driver dies pre-staging
    jid = w1.task_submit({"command": "echo ok > out.txt",
                          "outputs": ["out.txt"], "site": "local"})["job_id"]
    del w1

    # "poll": supervision only — honest stamp, NO mutation
    w2 = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="poll")
    w2.runner.poll_interval = 0.2
    row = w2.store.get_job(jid)
    assert row["state"] not in ("DONE", "FAILED", "CANCELLED")
    assert "reconcile() re-drives" in (row["queue_reason"] or "")
    assert _events(w2, "job.driver_lost"), "stamp must be an event too"
    del w2

    # "full": completes the user's original intent
    w3 = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="full")
    w3.runner.poll_interval = 0.2
    row = _wait_state(w3, jid, ("DONE",))
    assert row["redrives"] == 1


def test_redrive_cap_fails_honest(tmp_path, pixi_bin):
    w = _mkweft(tmp_path, pixi_bin, resume="off")
    w.runner._drive = lambda job_id: None       # every driver dies
    jid = w.task_submit({"command": "true", "site": "local"})["job_id"]
    for _ in range(w.runner.MAX_REDRIVES):
        acts = [a for a in w.runner.reconcile() if a["job"] == jid]
        assert acts and acts[0]["action"] == "re-drive"
    acts = [a for a in w.runner.reconcile() if a["job"] == jid]
    assert acts and acts[0]["action"] == "redrive-exhausted"
    row = w.store.get_job(jid)
    assert row["state"] == "FAILED"
    assert row["error"]["error"] == "job.redrive_exhausted"
    assert row["error"]["hints"]["redrives"] == w.runner.MAX_REDRIVES


# -- transport-outage verdicts: park, probe, adopt ----------------------------

def test_submit_reply_lost_parks_and_recovers(tmp_path, pixi_bin):
    """The exact shape aba watched live: rc-255 AFTER delivery — the
    node computes the true answer while (pre-fix) the row said FAILED."""
    w = _mkweft(tmp_path, pixi_bin)
    adapter = w.runner.adapters["local"]
    orig = adapter.submit

    def reply_lost(jobdir_rel, task):
        orig(jobdir_rel, task)      # the command DID start
        raise WeftError("site.unreachable", "transport failed",
                        stage="infra", retryable=True,
                        hints={"delivered": "unknown"})

    adapter.submit = reply_lost
    jid = w.task_submit({"command": "sleep 2 && echo ok > out.txt",
                         "outputs": ["out.txt"], "site": "local"})["job_id"]
    row = _wait_state(w, jid, ("DONE",), timeout=90)
    assert row["deferral"] is None, "deferral must clear on recovery"
    assert not _events(w, "job.failed"), \
        "a parked submit must never mint FAILED"
    deferred = _events(w, "job.deferred")
    assert deferred and deferred[0]["delivered"] == "unknown", \
        "delivered tri-state is the probe's discriminator — it must ride " \
        "the event"
    recovered = _events(w, "job.recovered")
    assert recovered and recovered[0]["found"] in ("running", "exited")


def test_submit_never_delivered_redrives_once(tmp_path, pixi_bin):
    w = _mkweft(tmp_path, pixi_bin)
    adapter = w.runner.adapters["local"]
    orig = adapter.submit
    calls = {"n": 0}

    def cut_before_delivery(jobdir_rel, task):
        calls["n"] += 1
        if calls["n"] == 1:
            raise WeftError("site.unreachable", "transport failed",
                            stage="submit", retryable=True,
                            hints={"delivered": "no"})
        return orig(jobdir_rel, task)

    adapter.submit = cut_before_delivery
    jid = w.task_submit({"command": "echo ok > out.txt",
                         "outputs": ["out.txt"], "site": "local"})["job_id"]
    row = _wait_state(w, jid, ("DONE",), timeout=90)
    assert calls["n"] == 2, "exactly one re-drive"
    assert len(_events(w, "job.redriven")) == 1
    assert row["deferral"] is None
    assert not _events(w, "job.failed")


def test_deferred_past_grace_fails_honest(tmp_path, pixi_bin):
    """Parked limbo is BOUNDED: a site that never returns yields the
    honest terminal verdict, with the deferral context and the lever."""
    w = _mkweft(tmp_path, pixi_bin,
                policy={"outage_requeue_grace_s": 0.6})
    adapter = w.runner.adapters["local"]

    def submit_dead(jobdir_rel, task):
        raise WeftError("site.unreachable", "transport failed",
                        stage="submit", retryable=True,
                        hints={"delivered": "unknown"})

    def shim_dead(argv, timeout=60.0):
        raise WeftError("site.unreachable", "transport failed",
                        stage="infra", retryable=True)

    adapter.submit = submit_dead
    adapter.shim = shim_dead        # probes can't answer either
    jid = w.task_submit({"command": "true", "site": "local"})["job_id"]
    row = _wait_state(w, jid, ("FAILED",), timeout=60)
    err = row["error"]
    assert err["error"] == "site.unreachable" and err["stage"] == "submit"
    assert err["hints"]["deferred_for_s"] >= 0.6
    assert "outage_requeue_grace_s" in err["hints"]["lever"]


def test_walltime_verdict_survives_dead_cancel_transport(tmp_path, pixi_bin):
    """Pre-fix this minted FAILED(site.unreachable): the per-job catch
    fed a transport failure from adapter.cancel straight to _fail. The
    honest verdict must land once transport recovers."""
    w = _mkweft(tmp_path, pixi_bin)
    adapter = w.runner.adapters["local"]
    orig_cancel = adapter.cancel
    fails = {"n": 0}

    def flaky_cancel(handle, jobdir_rel):
        fails["n"] += 1
        if fails["n"] <= 2:
            raise WeftError("site.unreachable", "transport failed",
                            stage="infra", retryable=True)
        return orig_cancel(handle, jobdir_rel)

    jid = w.task_submit({"command": "sleep 60",
                         "resources": {"walltime": "0:01"},
                         "site": "local"})["job_id"]
    _wait_state(w, jid, ("RUNNING",), timeout=60)
    adapter.cancel = flaky_cancel
    row = _wait_state(w, jid, ("FAILED",), timeout=90)
    assert row["error"]["error"] == "job.walltime_exceeded", row["error"]
    assert fails["n"] >= 3, "cancel must have been retried through the outage"


# -- cross-process collection claim -------------------------------------------

def test_two_resuming_instances_collect_once(tmp_path, pixi_bin):
    """Auto-resume means several controllers can watch one exited orphan;
    the store-level claim must let exactly ONE collect (double collection
    doubles events and races ingestion)."""
    jid = _orphan_job(tmp_path, pixi_bin, "echo done > out.txt",
                      kill_after=1.0)
    jobdir = tmp_path / "site" / "jobs" / jid
    deadline = time.time() + 60
    while not (jobdir / "exit_code").exists() and time.time() < deadline:
        time.sleep(0.2)

    ws = []

    def boot():
        wx = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="poll")
        wx.runner.poll_interval = 0.2
        ws.append(wx)

    t1 = threading.Thread(target=boot)
    t2 = threading.Thread(target=boot)
    t1.start(); t2.start(); t1.join(); t2.join()
    row = _wait_state(ws[0], jid, ("DONE",))
    assert row["manifest"]
    done = _events(ws[0], "job.done")
    collecting = [e for e in _events(ws[0], "job.state")
                  if e.get("state") == "COLLECTING" and e["job_id"] == jid]
    assert len(done) == 1, f"collected {len(done)} times"
    assert len(collecting) == 1, "both instances entered collection"


def test_collect_claim_is_atomic_and_stale_recoverable(tmp_path, pixi_bin):
    w = _mkweft(tmp_path, pixi_bin, resume="off")
    jid = w.task_submit({"command": "true", "site": "local"})["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    st = w.store
    # terminal jobs are not claimable (a peer finished the whole collect)
    assert st.claim_job_collect(jid, "n1") is False
    st._write("UPDATE jobs SET state='RUNNING' WHERE job_id=?", (jid,))
    assert st.claim_job_collect(jid, "n1") is True
    assert st.claim_job_collect(jid, "n2") is False      # held, fresh
    st.release_job_collect(jid, "wrong-nonce")           # nonce-scoped no-op
    assert st.claim_job_collect(jid, "n3") is False
    st.release_job_collect(jid, "n1")
    assert st.claim_job_collect(jid, "n4") is True       # released -> free
    st._write("UPDATE jobs SET collect_hb=? WHERE job_id=?",
              (time.time() - 600, jid))
    assert st.claim_job_collect(jid, "n5") is True       # stale -> takeover


# -- scheduler discrimination (unit, stubbed transport) -----------------------

def test_slurm_find_handle_by_name_discriminates(monkeypatch, tmp_path):
    from weft.adapters.slurm import SlurmAdapter
    a = SlurmAdapter.__new__(SlurmAdapter)
    a.poll_timeout = 10.0
    a.name = "hpc"      # subject sweep: refusals name their site

    class R:
        def __init__(self, rc, out, err=""):
            self.rc, self.out, self.err = rc, out, err

    a.run_cmd = lambda cmd, timeout=None: R(0, "12345\n")
    assert a.find_handle_by_name("weft-jb_x") == "slurm:12345"
    a.run_cmd = lambda cmd, timeout=None: R(0, "")
    assert a.find_handle_by_name("weft-jb_x") is None
    a.run_cmd = lambda cmd, timeout=None: R(1, "", "slurmctld down")
    with pytest.raises(WeftError) as ei:
        a.find_handle_by_name("weft-jb_x")
    assert ei.value.code == "site.unreachable"
    assert "hpc" in ei.value.detail    # the refusal names its site
