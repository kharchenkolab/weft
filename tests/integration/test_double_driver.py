"""#71 — the double-driver race, deterministically.

Field evidence (flake ledger 2026-07-20): a fresh process's reconcile()
re-drove a job whose ORIGINAL driver thread was still alive (del never
joins daemon threads) — two drivers staged one jobdir, and since
_prepare_sandbox begins with a jobdir wipe, one driver's cmd.sh vanished
under the other's submit ("run: need --dir with cmd.sh", rc=0 job
FAILED). "Stages are idempotent" holds against a DEAD predecessor only.

Fix: a store-level drive claim with heartbeat — sqlite is the one truth
shared across threads, Weft instances, and processes. A live claim makes
reconcile stand down honestly; a stale claim (dead driver) is broken by
the next claimant's conditional update."""

import time

from weft.api import Weft


def _w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def test_live_ghost_driver_is_not_raced(tmp_path, pixi_bin):
    """The exact field interleave, with the window held open: the ghost
    is mid-staging when the fresh process reconciles. One driver, one
    staging, correct output."""
    w1 = _w(tmp_path, pixi_bin)
    orig = w1.runner._prepare_sandbox

    def slow_prepare(*a, **k):
        time.sleep(3.0)          # hold the race window WIDE open
        return orig(*a, **k)

    w1.runner._prepare_sandbox = slow_prepare
    r = w1.task_submit({"command": "echo fin > results/x.txt",
                        "outputs": ["results/"], "site": "local"})
    jid = r["job_id"]
    time.sleep(0.8)              # ghost thread has claimed, is in the sleep
    del w1                       # daemon threads live on — the GHOST

    w2 = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w2.runner.poll_interval = 0.2
    actions = [a for a in w2.runner.reconcile() if a["job"] == jid]
    assert actions and actions[0]["action"] == "driving-elsewhere", actions

    job = w2.runner.wait(jid, 120)
    assert job["state"] == "DONE", job.get("error")
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/x.txt")
    assert out["preview"]["lines"] == ["fin"]
    # exactly ONE staging ever happened — no second driver touched it
    staged = [e for e in w2.events_poll(0, 800)["events"]
              if e["kind"] == "job.staged"]
    assert len([e for e in staged if e.get("job_id") == jid]) == 1


def test_stale_claim_is_broken_and_re_driven(tmp_path, pixi_bin):
    """A claim whose holder DIED (no heartbeat) must not wedge the job:
    reconcile re-drives and the conditional claim takes over."""
    w1 = _w(tmp_path, pixi_bin)
    w1.runner._drive = lambda job_id: None     # driver dies before staging
    r = w1.task_submit({"command": "echo ok > results/y.txt",
                        "outputs": ["results/"], "site": "local"})
    jid = r["job_id"]
    # the corpse: a claim with a long-dead heartbeat
    w1.store._write(
        "UPDATE jobs SET driver_nonce='dead-000000', driver_hb=? "
        "WHERE job_id=?", (time.time() - 600, jid))
    del w1

    w2 = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w2.runner.poll_interval = 0.2
    actions = [a for a in w2.runner.reconcile() if a["job"] == jid]
    assert actions and actions[0]["action"] == "re-drive", actions
    job = w2.runner.wait(jid, 120)
    assert job["state"] == "DONE", job.get("error")


def test_claim_is_atomic_and_nonce_scoped(tmp_path, pixi_bin):
    w = _w(tmp_path, pixi_bin)
    r = w.task_submit({"command": "true", "site": "local"})
    jid = r["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    st = w.store
    st.release_job_drive(jid, "whoever")       # nonce-scoped: no-op
    assert st.claim_job_drive(jid, "n1") is True
    assert st.claim_job_drive(jid, "n2") is False     # held, fresh
    st.heartbeat_job_drive(jid, "n2")                 # wrong nonce: no-op
    c = st.job_drive_claim(jid)
    assert c["nonce"] == "n1"
    st.release_job_drive(jid, "n2")                   # wrong nonce: no-op
    assert st.job_drive_claim(jid) is not None
    st.release_job_drive(jid, "n1")
    assert st.job_drive_claim(jid) is None
    assert st.claim_job_drive(jid, "n2") is True      # free again
