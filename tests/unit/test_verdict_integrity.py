"""Round A (2026-07 sweep): verdicts must come from evidence, not from
silence. A scheduler outage is not 'job departed'; a failed probe is not
'no partitions'; an unconfirmed scancel is not CANCELLED."""

import pytest

from weft.adapters.base import ShimResult
from weft.adapters.slurm import SlurmAdapter
from weft.errors import WeftError


def _slurm(answers):
    ad = SlurmAdapter("hpc", "login", "/site/root", user="u")

    def fake_run(cmd, *, input_bytes=None, timeout=120.0):
        for key, resp in answers.items():
            if key in cmd:
                return resp
        return ShimResult(0, "", "")

    ad._run = fake_run
    return ad


def test_departed_job_is_not_an_outage():
    """rc 1 + 'Invalid job id' is the routine departed answer (probed on
    real slurm) — poll proceeds to the file fallback."""
    ad = _slurm({"squeue": ShimResult(1, "", "slurm_load_jobs error: "
                                             "Invalid job id specified"),
                 "scontrol": ShimResult(1, "", "Invalid job id specified"),
                 "weft-shim": ShimResult(0, '{"state": "unknown"}', "")})
    st = ad.poll_job("slurm:12345", "jobs/j1")
    assert st["state"] == "unknown"          # files consulted, no raise


def test_control_plane_outage_raises_unreachable():
    """rc≠0 WITHOUT the invalid-id marker is the control plane failing —
    routed to the poller's outage machinery, never a lost-strike."""
    ad = _slurm({"squeue": ShimResult(1, "", "slurm_load_jobs error: "
                                             "Unable to contact slurm "
                                             "controller")})
    with pytest.raises(WeftError) as ei:
        ad.poll_job("slurm:12345", "jobs/j1")
    assert ei.value.code == "site.unreachable" and ei.value.retryable
    assert "Unable to contact" in ei.value.hints["stderr"]


def test_batched_poll_outage_raises_too():
    ad = _slurm({"squeue": ShimResult(1, "", "socket timed out")})
    with pytest.raises(WeftError) as ei:
        ad.poll_jobs([("slurm:1", "jobs/a"), ("slurm:2", "jobs/b")])
    assert ei.value.code == "site.unreachable"


def test_mixed_batch_with_departed_ids_is_normal():
    """Real-slurm probe: mixed valid+purged ids exit 0 with valid rows
    intact and the error on stderr — must parse the rows."""
    ad = _slurm({"squeue": ShimResult(
                     0, "96|RUNNING|None\n",
                     "slurm_load_jobs error: Invalid job id specified"),
                 "scontrol": ShimResult(1, "", "Invalid job id specified"),
                 "weft-shim": ShimResult(0, "", "")})
    out = ad.poll_jobs([("slurm:96", "jobs/a"), ("slurm:999", "jobs/b")])
    assert out["slurm:96"]["state"] == "running"


def test_sinfo_probe_failure_is_not_no_partitions():
    ad = _slurm({"sinfo -h -o": ShimResult(
        1, "", "slurm_load_partitions error: Unable to contact slurm "
               "controller")})
    with pytest.raises(WeftError) as ei:
        ad._probe_partitions()
    assert ei.value.code == "site.unreachable"
    assert "refusing to record" in ei.value.detail


def test_sinfo_probe_success_dedups_rows():
    rows = "p1|1-00:00:00|8|64000|up|(null)|f1|4\n" * 3 \
         + "p2|2-00:00:00|16|128000|up|(null)|f2|8\n"
    ad = _slurm({"sinfo -h -o": ShimResult(0, rows, ""),
                 "scontrol show partition": ShimResult(0, "", ""),
                 "sinfo --version": ShimResult(0, "slurm 23.02.6", "")})
    got = ad._probe_partitions()
    names = sorted(p["name"] for p in got["partitions"])
    assert names == ["p1", "p2"]


# ── scancel confirm-then-unregister ────────────────────────────────────────

class _FakeRunner:
    def __init__(self, store):
        self.store = store

    def group_payload(self, group):
        return {}


def test_cancel_waits_for_scheduler_agreement(tmp_path):
    from weft.poller import SitePoller, Watch
    from weft.store import Store
    from weft.task import Task

    store = Store(tmp_path / "s.db")
    events = []
    cancels = []

    class _Ad:
        name = "hpc"

        def cancel(self, handle, rel):
            cancels.append(handle)

        def path(self, rel):
            return f"/x/{rel}"

    p = SitePoller.__new__(SitePoller)
    p.adapter = _Ad()
    p.runner = _FakeRunner(store)
    p.site = "hpc"
    import threading
    p._lock = threading.Lock()
    p._watches = {}
    t = Task.from_dict({"command": "true"})
    w = Watch(job_id="j1", handle="slurm:1", jobdir_rel="jobs/j1", task=t,
              started_at=0.0, scheduler=True)
    w.cancelled = True
    p._watches["j1"] = w

    # tick 1: send the cancel, KEEP the watch
    p._transition(w, {"state": "running"})
    assert cancels == ["slurm:1"] and "j1" in p._watches
    # tick 2: scheduler still says running -> resend, still watching
    p._transition(w, {"state": "running"})
    assert len(cancels) == 2 and "j1" in p._watches
    # tick 3: scheduler agrees -> unregistered
    p._transition(w, {"state": "cancelled"})
    assert "j1" not in p._watches
