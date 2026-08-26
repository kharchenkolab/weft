"""R1b: the deferral x ephemeral-site deadlock, pinned at machine
cadence without docker. A parked submit's probe only re-drove after
the site ANSWERED with an empty jobdir — but an ephemeral site (cloud
instance, torn-down container) only exists when a drive provisions it,
so 'answers' needed the re-drive and the re-drive waited on 'answers':
both R1b docker tests sat STAGING against the 3600s grace. Contract
now: consecutive dead-transport probe ticks re-drive ONCE, but ONLY
when the deferral proves the submit was never attempted (the submit is
the one call whose loss can leave a live remote run); attempts and
submit_attempted both gate, and submit_attempted is sticky across
re-parks."""

import pytest

from weft.api import Weft
from weft.errors import WeftError
from weft.poller import UNREACHABLE_REDRIVE_STRIKES, Watch
from weft.task import Task


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def _park(w, job_id, *, submit_attempted, attempts=0):
    import time
    now = time.time()   # a 1970 anchor would trip the 3600s grace
    task = {"command": "true", "site": "local"}
    w.store.put_job(job_id, "t" * 64, task, "local", "STAGING")
    w.store.set_job_deferral(job_id, {
        "since": now, "stage": "infra", "delivered": "no",
        "submit_attempted": submit_attempted, "attempts": attempts})
    return Watch(job_id=job_id, handle=f"probe:{job_id}",
                 jobdir_rel=f"jobs/{job_id}",
                 task=Task.from_dict(task), started_at=now,
                 scheduler=False, last_state="STAGING", probe=True,
                 deferred_since=now)


def _dead_transport(monkeypatch, poller):
    def dead(argv, timeout=60.0):
        raise WeftError("site.unreachable", "transport down",
                        stage="infra", retryable=True)
    monkeypatch.setattr(poller.adapter, "shim", dead)


def _redriven(w):
    return [e for e in w.store.events_since(0, limit=500)
            if e["kind"] == "job.redriven"]


def test_unreachable_probe_redrives_once_when_submit_never_ran(
        w, monkeypatch):
    p = w.runner.poller_for("local")
    _dead_transport(monkeypatch, p)
    driven = []
    monkeypatch.setattr(w.runner, "_drive", lambda jid: driven.append(jid))
    watch = _park(w, "jb_probe000001", submit_attempted=False)
    p.register(watch)
    for i in range(UNREACHABLE_REDRIVE_STRIKES):
        assert driven == [], f"re-drove before strike {i + 1}"
        p._tick([watch])
    # the re-drive thread is spawned inside the tick; join via the spy
    deadline_ticks = 100
    import time
    while not driven and deadline_ticks:
        time.sleep(0.02)
        deadline_ticks -= 1
    assert driven == ["jb_probe000001"]
    ev = _redriven(w)
    assert ev and "never attempted" in ev[-1]["note"]
    dfr = w.store.get_job("jb_probe000001")["deferral"]
    assert dfr["attempts"] == 1, "the existing attempts guard must arm"
    # the watch left the poller: the drive owns the job now
    assert "jb_probe000001" not in p._watches


def test_no_redrive_when_submit_was_attempted(w, monkeypatch):
    """delivered-unknown submits keep today's semantics: jobdir truth
    decides when the site answers — a blind re-drive could run the
    task twice (the aba live incident: rc-255 with the reply lost,
    node computing the true answer under a FAILED row)."""
    p = w.runner.poller_for("local")
    _dead_transport(monkeypatch, p)
    driven = []
    monkeypatch.setattr(w.runner, "_drive", lambda jid: driven.append(jid))
    watch = _park(w, "jb_probe000002", submit_attempted=True)
    p.register(watch)
    for _ in range(UNREACHABLE_REDRIVE_STRIKES + 2):
        p._tick([watch])
    assert driven == [] and not _redriven(w)
    assert "jb_probe000002" in p._watches, "the watch must stay parked"


def test_attempts_exhausted_lands_terminal_verdict_not_hour_park(
        w, monkeypatch):
    """After the ONE re-drive was also cut pre-submit, continued dead
    transport must produce the honest FAILED verdict at strike-out —
    the first fix version fell back to parking against the 3600s grace
    (burst lane: deferred -> redriven -> deferred -> hour-long park
    while the test timed out at 300s)."""
    p = w.runner.poller_for("local")
    _dead_transport(monkeypatch, p)
    driven = []
    monkeypatch.setattr(w.runner, "_drive", lambda jid: driven.append(jid))
    watch = _park(w, "jb_probe000003", submit_attempted=False, attempts=1)
    p.register(watch)
    for _ in range(UNREACHABLE_REDRIVE_STRIKES + 2):
        p._tick([watch])
    assert driven == [] and not _redriven(w), "never a SECOND re-drive"
    job = w.store.get_job("jb_probe000003")
    assert job["state"] == "FAILED"
    assert job["error"]["error"] == "site.unreachable"
    assert "re-drive was cut" in job["error"]["detail"]
    assert job["error"]["retryable"] is True
    assert "jb_probe000003" not in p._watches


def test_answered_tick_resets_the_strike_streak(w, monkeypatch):
    """Strikes are CONSECUTIVE dead-transport ticks: one answered tick
    (empty jobdir — a normal absent strike) restarts the streak, so a
    flapping link cannot accumulate its way into a re-drive."""
    p = w.runner.poller_for("local")
    driven = []
    monkeypatch.setattr(w.runner, "_drive", lambda jid: driven.append(jid))
    watch = _park(w, "jb_probe000004", submit_attempted=False)
    p.register(watch)
    _dead_transport(monkeypatch, p)
    for _ in range(UNREACHABLE_REDRIVE_STRIKES - 1):
        p._tick([watch])
    assert watch.unreachable_strikes == UNREACHABLE_REDRIVE_STRIKES - 1

    class _Answer:
        def json(self):
            return {"state": "missing"}
    monkeypatch.setattr(p.adapter, "shim",
                        lambda argv, timeout=60.0: _Answer())
    p._tick([watch])
    assert watch.unreachable_strikes == 0
    assert driven == []


def test_submit_attempted_is_sticky_across_reparks(w):
    """A re-drive cut at STAGING must not erase the duplicate risk the
    first drive's cut submit created."""
    job_id = "jb_probe000005"
    w.store.put_job(job_id, "t" * 64,
                    {"command": "true", "site": "local"}, "local",
                    "STAGING")
    first = WeftError("site.unreachable", "cut at submit", stage="infra",
                      retryable=True,
                      hints={"submit_attempted": True,
                             "delivered": "unknown"})
    w.runner._defer_job(job_id, first)
    second = WeftError("site.unreachable", "cut at staging",
                       stage="staging", retryable=True,
                       hints={"stderr": "kex_exchange_identification: "
                                        "Connection closed"})
    w.runner._defer_job(job_id, second)
    dfr = w.store.get_job(job_id)["deferral"]
    assert dfr["submit_attempted"] is True
    # the park carries WHAT cut it (R1b triage: stage/delivered alone
    # forced a lane re-run per hypothesis)
    assert dfr["cut"] == "cut at staging"
    assert "kex_exchange" in dfr["stderr"]
    ev = [e for e in w.store.events_since(0, limit=200)
          if e["kind"] == "job.deferred"][-1]
    assert ev["cut"] == "cut at staging" and "kex" in ev["stderr"]
