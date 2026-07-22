"""SitePoller transition semantics, driven synchronously with a scripted
adapter — outages, lost strikes, walltime, cancel, collection parking."""

import time

import pytest

from weft.api import Weft
from weft.errors import WeftError
from weft.poller import SitePoller, Watch
from weft.task import Task


class ScriptedAdapter:
    """poll_jobs returns queued scripts; records cancels."""

    name = "fake"

    def __init__(self):
        self.script: list = []   # each entry: dict {handle: status} or WeftError
        self.cancelled: list = []

    def poll_jobs(self, items):
        step = self.script.pop(0)
        if isinstance(step, WeftError):
            raise step
        return {h: step.get(h, {"state": "unknown"}) for h, _ in items}

    def cancel(self, handle, jobdir_rel):
        self.cancelled.append(handle)

    def path(self, rel):
        return f"/fake/{rel}"

    def shim(self, argv, timeout=60):
        class R:
            rc, out, err = 0, "", ""
        return R()


@pytest.fixture
def rig(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    adapter = ScriptedAdapter()
    w.adapters["fake"] = adapter
    w.store.put_site("fake", "ssh", {})
    poller = SitePoller("fake", adapter, w.runner)
    collected = []
    w.runner.enqueue_collect = lambda watch, status: collected.append(
        (watch.job_id, status))
    return w, adapter, poller, collected


def _watch(w, job_id="jb_x", scheduler=False, walltime="", group=None,
           started_at=None):
    task = Task.from_dict({"command": "true", "site": "fake",
                           "resources": {"walltime": walltime}})
    w.store.put_job(job_id, task.task_hash(), task.to_dict(), "fake", "RUNNING",
                    array_group=group)
    return Watch(job_id=job_id, handle=f"h:{job_id}", jobdir_rel=f"jobs/{job_id}",
                 task=task, started_at=started_at or time.time(),
                 scheduler=scheduler, array_group=group, last_state="RUNNING")


def test_exited_goes_to_collector(rig):
    w, adapter, poller, collected = rig
    watch = _watch(w)
    poller.register(watch)
    adapter.script = [{watch.handle: {"state": "exited", "exit_code": 0}}]
    poller._tick([watch])
    assert collected == [(watch.job_id, {"state": "exited", "exit_code": 0})]
    assert not poller.watching(watch.job_id)


def test_queued_to_running_transition_event(rig):
    w, adapter, poller, _ = rig
    watch = _watch(w)
    watch.last_state = "QUEUED"
    w.store.update_job(watch.job_id, state="QUEUED")
    adapter.script = [{watch.handle: {"state": "running"}}]
    poller._tick([watch])
    assert w.store.get_job(watch.job_id)["state"] == "RUNNING"


def test_two_strikes_before_node_failure(rig):
    w, adapter, poller, _ = rig
    watch = _watch(w)
    poller.register(watch)
    adapter.script = [{watch.handle: {"state": "lost"}}]
    poller._tick([watch])
    assert w.store.get_job(watch.job_id)["state"] == "RUNNING"  # one strike: no verdict
    adapter.script = [{watch.handle: {"state": "lost"}}]
    poller._tick([watch])
    job = w.store.get_job(watch.job_id)
    assert job["state"] == "FAILED"
    assert job["error"]["error"] == "sched.node_failure"
    # a healthy poll in between resets the strikes
    watch2 = _watch(w, "jb_y")
    poller.register(watch2)
    adapter.script = [{watch2.handle: {"state": "lost"}},
                      {watch2.handle: {"state": "running"}},
                      {watch2.handle: {"state": "lost"}}]
    for _ in range(3):
        poller._tick([watch2])
    assert w.store.get_job("jb_y")["state"] == "RUNNING"
    assert watch2.lost_strikes == 1


def test_outage_is_one_event_and_jobs_survive(rig):
    w, adapter, poller, _ = rig
    watches = [_watch(w, f"jb_{i}") for i in range(5)]
    unreachable = WeftError("site.unreachable", "down", stage="infra",
                            retryable=True)
    adapter.script = [unreachable, unreachable, unreachable,
                      {w_.handle: {"state": "running"} for w_ in watches}]
    for _ in range(4):
        poller._tick(watches)
    events = w.store.events_since(0, 500)
    unreach = [e for e in events if e["kind"] == "site.unreachable"]
    reach = [e for e in events if e["kind"] == "site.reachable"]
    assert len(unreach) == 1, "one outage, one event — not one per tick or job"
    assert unreach[0]["jobs_waiting"] == 5
    assert len(reach) == 1 and reach[0]["outage_s"] >= 0
    assert all(w.store.get_job(w_.job_id)["state"] == "RUNNING" for w_ in watches)
    assert poller._backoff == 0.0  # reset after recovery


def test_controller_walltime_on_interactive_sites(rig):
    w, adapter, poller, _ = rig
    watch = _watch(w, walltime="00:00:01", started_at=time.time() - 60)
    poller.register(watch)
    adapter.script = [{watch.handle: {"state": "running"}}]
    poller._tick([watch])
    job = w.store.get_job(watch.job_id)
    assert job["state"] == "FAILED"
    assert job["error"]["error"] == "job.walltime_exceeded"
    assert adapter.cancelled == [watch.handle]
    # scheduler sites enforce their own limits: no controller kill there
    watch2 = _watch(w, "jb_sched", scheduler=True, walltime="00:00:01",
                    started_at=time.time() - 60)
    poller.register(watch2)
    adapter.script = [{watch2.handle: {"state": "running"}}]
    poller._tick([watch2])
    assert w.store.get_job("jb_sched")["state"] == "RUNNING"


def test_cancel_notification(rig):
    """Cancel is confirm-then-unregister: the watch stays until the
    scheduler agrees the job is gone (an unconfirmed scancel is not
    CANCELLED)."""
    w, adapter, poller, _ = rig
    watch = _watch(w)
    poller.register(watch)
    poller.notify_cancel(watch.job_id)
    adapter.script = [{watch.handle: {"state": "running"}},
                      {watch.handle: {"state": "cancelled"}}]
    poller._tick([watch])
    assert adapter.cancelled == [watch.handle]
    assert poller.watching(watch.job_id)      # sent, not yet confirmed
    poller._tick([watch])
    assert not poller.watching(watch.job_id)  # scheduler agreed


def test_collect_parks_job_during_outage(rig, monkeypatch):
    """Collection through a dead site defers back to the poller instead of
    failing the job — an outage costs waiting, never a wrong verdict."""
    import weft.runner as runner_mod
    w, adapter, poller, _ = rig
    monkeypatch.setattr(runner_mod, "COLLECT_RETRIES", 1)
    monkeypatch.setattr(runner_mod, "COLLECT_BACKOFF_S", 0.01)
    watch = _watch(w)
    w.runner._pollers["fake"] = poller

    def dead_collect(*a, **k):
        raise WeftError("site.unreachable", "down", stage="infra", retryable=True)
    monkeypatch.setattr(w.runner, "_collect", dead_collect)
    # as enqueue_collect would:
    w.runner._collecting.add(watch.job_id)
    w.store.update_job(watch.job_id, state="COLLECTING")
    w.runner._collect_guarded(watch, {"state": "exited", "exit_code": 0})
    assert poller.watching(watch.job_id), "job parked back with the poller"
    kinds = [e["kind"] for e in w.store.events_since(0, 100)]
    assert "collect.deferred" in kinds
    assert w.store.get_job(watch.job_id)["state"] == "COLLECTING"  # not FAILED
