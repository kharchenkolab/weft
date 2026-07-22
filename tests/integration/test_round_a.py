"""Round A integration: lease heartbeats keep live builders alive,
walltime anchors at submit (not creation), the shim stamps a same-clock
liveness epoch."""

import time

from weft.adapters.local import LocalAdapter
from weft.api import Weft
from weft.realize import _SiteLease


def _w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def test_lease_heartbeat_protects_live_builder(tmp_path, monkeypatch):
    ad = LocalAdapter("l", tmp_path)
    holder = _SiteLease(ad, "envs/e1")
    assert holder.acquire_or_adopt() is False    # we hold it, HB started
    hb = tmp_path / "envs/e1.lease/hb"
    assert hb.exists() and int(hb.read_text()) > 0

    # a waiter with an aggressive staleness must still see it LIVE —
    # the beat, not the build duration, is the liveness signal
    waiter = _SiteLease(ad, "envs/e1")
    monkeypatch.setattr(_SiteLease, "HB_STALE_S", 60)
    monkeypatch.setattr(_SiteLease, "MAX_WAIT_S", 1.5)
    monkeypatch.setattr(_SiteLease, "WAIT_S", 0.2)
    import pytest
    from weft.errors import WeftError
    with pytest.raises(WeftError) as ei:        # times out WAITING, no theft
        waiter.acquire_or_adopt()
    assert ei.value.code == "state.conflict"
    assert (tmp_path / "envs/e1.lease").exists()   # lease NOT stolen
    holder.release()
    assert not (tmp_path / "envs/e1.lease").exists()


def test_stale_heartbeat_is_taken_over(tmp_path, monkeypatch):
    ad = LocalAdapter("l", tmp_path)
    lease = tmp_path / "envs/e2.lease"
    lease.mkdir(parents=True)
    (lease / "hb").write_text(str(int(time.time()) - 3600))   # dead beat
    waiter = _SiteLease(ad, "envs/e2")
    monkeypatch.setattr(_SiteLease, "WAIT_S", 0.1)
    assert waiter.acquire_or_adopt() is False    # stale broken, we hold
    assert int((lease / "hb").read_text()) > time.time() - 60  # fresh beat
    waiter.release()


def test_walltime_anchor_is_submit_not_creation(tmp_path, pixi_bin):
    w = _w(tmp_path, pixi_bin)
    r = w.task_submit({"command": "sleep 2 && echo ok > o.txt",
                       "outputs": ["o.txt"], "site": "local"})
    jid = r["job_id"]
    time.sleep(0.8)                              # job submitted by now
    job = w.store.get_job(jid)
    assert job["submitted_at"] and job["submitted_at"] >= job["created_at"]

    # a fresh process reconciles: the watch must anchor at SUBMIT — a
    # created_at anchor charged staging time against the run (false
    # walltime kills after controller restarts)
    del w
    w2 = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w2.runner.poll_interval = 0.2
    w2.runner.reconcile()
    watch = w2.runner.poller_for("local")._watches.get(jid)
    if watch is not None:                        # may already have exited
        assert watch.started_at == job["submitted_at"]
    assert w2.runner.wait(jid, 120)["state"] == "DONE"


def test_shim_stamps_node_clock_epoch(tmp_path, pixi_bin):
    w = _w(tmp_path, pixi_bin)
    r = w.task_submit({"command": "sleep 1 && echo x > o.txt",
                       "outputs": ["o.txt"], "site": "local"})
    time.sleep(0.8)
    epoch = tmp_path / "site" / "jobs" / r["job_id"] / "pid.epoch"
    assert epoch.exists()
    assert abs(int(epoch.read_text()) - time.time()) < 30
    assert w.runner.wait(r["job_id"], 120)["state"] == "DONE"
