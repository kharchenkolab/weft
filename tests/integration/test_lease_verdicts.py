"""Round 13: scheduler verdicts kill leases immediately (weft-ui bug).

timeout/oom/cancelled are POSITIVE scheduler verdicts — unlike
lost/missing (absence of signal) they need no confirming strikes. The
old code let them fall through to the strike-reset line, so a
walltime-killed slurm kernel stayed "running" for as long as slurm
remembered the job. These tests fake the adapter's poll verdict on a
LOCAL lease, so the fix is covered in the fast lane; the slurm-fixture
test covers the real path end to end.
"""

import time

import pytest

from weft.api import Weft

SERVER = ("python3 -c 'import http.server, os; "
          "http.server.test(HandlerClass=http.server.SimpleHTTPRequestHandler,"
          'port=int(os.environ["WEFT_PORT"]), bind="127.0.0.1")\'')


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def _fake_verdict(monkeypatch, adapter, verdict):
    def poll_jobs(items):
        return {h: dict(verdict) for h, _ in items}
    monkeypatch.setattr(adapter, "poll_jobs", poll_jobs)


def _wait_state(get, want, timeout=30):
    for _ in range(int(timeout / 0.2)):
        if get() == want:
            return True
        time.sleep(0.2)
    return False


def test_timeout_verdict_kills_kernel_immediately(w, monkeypatch):
    k = w.kernel_start("local", "python",
                       label="phonon exploration")["kernel_id"]
    assert w.kernel_exec(k, "x = 1")["rc"] == 0
    krow = w.store.get_kernel(k)
    _fake_verdict(monkeypatch, w.adapters["local"],
                  {"state": "timeout", "slurm": "TIMEOUT"})
    assert _wait_state(lambda: w.store.get_kernel(k)["state"], "died"), \
        w.store.get_kernel(k)
    ev = next(e for e in w.events_poll(0, 500, compact=False)["events"]
              if e["kind"] == "kernel.died" and e["kernel"] == k)
    assert ev["cause"] == "walltime_exceeded"
    assert ev["slurm_state"] == "TIMEOUT"
    assert "walltime" in ev["suggestion"]
    assert ev["label"] == "phonon exploration"  # the death card can say WHAT died
    # cleanup: the fake verdict never killed the real driver
    monkeypatch.undo()
    w.adapters["local"].cancel(krow["handle"], krow["jobdir"])


def test_cancelled_verdict_kills_service_immediately(w, tmp_path, monkeypatch):
    r = w.service_start("local", {"command": SERVER}, ports=[18477],
                        ready_timeout=30)
    assert r["state"] == "ready", r
    sid = r["service_id"]
    srow = w.store.get_service(sid)
    _fake_verdict(monkeypatch, w.adapters["local"],
                  {"state": "cancelled", "slurm": "CANCELLED"})
    assert _wait_state(lambda: w.store.get_service(sid)["state"], "exited"), \
        w.store.get_service(sid)
    ev = next(e for e in w.events_poll(0, 500, compact=False)["events"]
              if e["kind"] == "service.exited" and e["service"] == sid)
    assert ev["cause"] == "cancelled" and ev["slurm_state"] == "CANCELLED"
    monkeypatch.undo()
    w.adapters["local"].cancel(srow["handle"], srow["jobdir"])


def test_oom_verdict_carries_cause(w, monkeypatch):
    k = w.kernel_start("local", "python")["kernel_id"]
    krow = w.store.get_kernel(k)
    _fake_verdict(monkeypatch, w.adapters["local"],
                  {"state": "oom", "slurm": "OUT_OF_MEMORY"})
    assert _wait_state(lambda: w.store.get_kernel(k)["state"], "died")
    ev = next(e for e in w.events_poll(0, 500, compact=False)["events"]
              if e["kind"] == "kernel.died" and e["kernel"] == k)
    assert ev["cause"] == "oom" and ev["slurm_state"] == "OUT_OF_MEMORY"
    monkeypatch.undo()
    w.adapters["local"].cancel(krow["handle"], krow["jobdir"])


def test_lost_still_needs_strikes(w, monkeypatch):
    """The strike guard stays for absence-of-signal states: one lost poll
    must NOT kill a kernel (that is an outage blip, not a verdict)."""
    k = w.kernel_start("local", "python")["kernel_id"]
    krow = w.store.get_kernel(k)
    calls = {"n": 0}
    real = w.adapters["local"].poll_jobs

    def flaky(items):
        calls["n"] += 1
        if calls["n"] == 1:
            return {h: {"state": "lost"} for h, _ in items}
        return real(items)
    monkeypatch.setattr(w.adapters["local"], "poll_jobs", flaky)
    deadline = time.time() + 3
    while time.time() < deadline:
        assert w.store.get_kernel(k)["state"] == "running"
        time.sleep(0.2)
    monkeypatch.undo()
    w.adapters["local"].cancel(krow["handle"], krow["jobdir"])


def test_kernel_label_surfaces_and_restart_inherits(w):
    r = w.kernel_start("local", "python", label="phonon exploration")
    k = r["kernel_id"]
    assert w.kernel_status(k)["label"] == "phonon exploration"
    row = next(x for x in w.list_kernels()["kernels"]
               if x["kernel_id"] == k)
    assert row["label"] == "phonon exploration"
    assert w.kernel_exec(k, "acc = 7")["rc"] == 0

    fresh = w.kernel_restart(k, replay="successful")
    k2 = fresh["kernel_id"]
    try:
        # the label names the WORK — the successor inherits it
        assert w.kernel_status(k2)["label"] == "phonon exploration"
        assert w.kernel_exec(k2, "print(acc)")["out"].strip() == "7"
    finally:
        w.kernel_stop(k2)

    # unlabeled reads None; oversized refused
    r2 = w.kernel_start("local", "python")
    assert w.kernel_status(r2["kernel_id"])["label"] is None
    w.kernel_stop(r2["kernel_id"])
    bad = w.kernel_start("local", "python", label="x" * 201)
    assert bad["error"] == "task.invalid" and "label" in bad["detail"]
