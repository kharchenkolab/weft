"""Live load awareness (what's free *now*) and array digest events."""

import time

import pytest

from weft.api import Weft

pytestmark = pytest.mark.docker


@pytest.fixture
def w(tmp_path, pixi_bin, slurm_site):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("hpc", "slurm", {
        "host": slurm_site["host"], "port": slurm_site["port"],
        "user": slurm_site["user"], "ssh_opts": slurm_site["ssh_opts"],
        "root": slurm_site["root"], "pixi_source": pixi_bin,
    })
    w.runner.poll_interval = 0.4
    return w


def test_slurm_load_view_reflects_queue_pressure(w):
    # The fixture cluster is shared across the suite and slurmd needs a
    # moment to register its node. Drain, then wait for a genuinely idle
    # node — and if it never comes, say WHAT the cluster looked like
    # instead of failing on a bare assert (this test flaked once and gave
    # no diagnosis; that was the actual bug).
    w.adapters["hpc"].run_cmd("scancel -u $USER 2>/dev/null; true")
    quiet = None
    for _ in range(180):
        quiet = w.site_load("hpc", fresh=True)
        std = quiet["partitions"].get("standard", {})
        if std.get("cpus_total") == 8 and std.get("cpus_idle") == 8:
            break
        time.sleep(1)
    else:
        squeue = w.adapters["hpc"].run_cmd("squeue -a; sinfo -N -l").out
        pytest.fail(
            "cluster never became idle within 180s.\n"
            f"last load view: {quiet['partitions']}\n{squeue}")
    assert "load_fraction" in quiet
    assert quiet["qos"] is None  # no accounting DB on the fixture — honest

    # hog the node, then queue one more: pressure must become visible
    r1 = w.task_submit({"command": "sleep 30", "resources": {"cpus": 8},
                        "site": "hpc"})
    r2 = w.task_submit({"command": "sleep 5", "resources": {"cpus": 4},
                        "site": "hpc"})
    assert "job_id" in r1 and "job_id" in r2
    for _ in range(120):
        st = {s["state"] for s in (w.task_status(r1["job_id"])
                                   + w.task_status(r2["job_id"]))}
        if "RUNNING" in st and "QUEUED" in st:
            break
        time.sleep(0.25)
    busy = w.site_load("hpc", fresh=True)
    std = busy["partitions"]["standard"]
    assert std["cpus_idle"] == 0, busy
    assert std["pending_jobs"] >= 1
    assert busy["my_jobs"]["running"] >= 1 and busy["my_jobs"]["pending"] >= 1

    est = w.site_load("hpc", resources={"cpus": 1, "walltime": "00:05:00"},
                      fresh=True)
    assert "start_estimate" in est  # scheduler ETA under current load
    w.task_cancel(r1["job_id"])
    w.task_cancel(r2["job_id"])


def test_array_digests_and_compact_feed(w):
    n = 8
    r = w.task_submit({
        "command": "test \"$WEFT_ARRAY_INDEX\" -eq 3 && exit 7; "
                   "echo ok > results/o.txt",
        "outputs": ["results/"],
        "resources": {"cpus": 1, "walltime": "00:05:00"},
        "site": "hpc", "array": n,
    })
    group = r["group"]
    for sub in r["jobs"]:
        w.runner.wait(sub["job_id"], 300)

    st = w.array_status(group)
    assert st["total"] == n and st["done"] == n - 1 and st["failed"] == 1
    assert st["failed_previews"][0]["index"] == 3
    assert st["failed_previews"][0]["code"] == "job.nonzero_exit"

    feed = w.events_poll(0, limit=500)  # compact by default
    kinds = [e["kind"] for e in feed["events"]]
    assert "array.progress" in kinds and "array.done" in kinds
    # element-level job.* events are digested away in compact mode
    assert not any(e["kind"].startswith("job.") and e.get("array_group")
                   for e in feed["events"])
    # digests are coalesced: far fewer than per-element events would be
    assert kinds.count("array.progress") <= 3 * n

    done = next(e for e in feed["events"] if e["kind"] == "array.done")
    assert done["done"] == n - 1 and done["failed"] == 1
    assert done["failures"][0]["index"] == 3
    assert "wall_s" in done and done["output_bytes"] > 0

    full = w.events_poll(0, limit=500, compact=False)
    assert any(e["kind"] == "job.done" and e.get("array_group") == group
               for e in full["events"])

    roll = w.array_result(group)
    assert roll["elements"]["3"]["state"] == "FAILED"
