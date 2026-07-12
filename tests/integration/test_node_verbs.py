"""Round C: the login→node hop as first-class verbs — probe-jobs write
per-partition compute records (measured node egress, GPUs, glibc), and
job_node_exec runs diagnostics inside a live allocation."""

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


def test_probe_deep_records_compute_truth(w):
    r = w.site_probe_deep("hpc", partitions=["standard", "gpu"])
    assert r["partitions"]["standard"]["ok"], r
    assert r["partitions"]["gpu"]["ok"], r
    # measured from a NODE: egress, GPUs (fake nvidia-smi), glibc
    std = r["partitions"]["standard"]
    assert std["internet"] is True         # docker has egress
    assert any(g["model"] == "Fake A100" for g in std["gpus"])

    caps = w.sites_describe("hpc")["capabilities"]
    parts = {p["name"]: p for p in caps["scheduler"]["partitions"]}
    assert parts["standard"]["compute"]["schema"] == "capabilities:v2"
    assert parts["standard"]["compute"]["internet"] is True
    # the site-level compute view now reflects a measured node record
    assert caps["compute"]["measured_on"] == \
        parts["standard"]["compute"]["measured_on"]


def test_node_exec_joins_a_running_allocation(w):
    job = w.task_submit({"command": "sleep 25",
                         "resources": {"cpus": 1}, "site": "hpc"})["job_id"]
    deadline = time.time() + 90
    while time.time() < deadline:
        if w.task_status(job)[0]["state"] == "RUNNING":
            break
        time.sleep(0.5)
    r = w.job_node_exec(job, "hostname; nvidia-smi --query-gpu="
                        "utilization.gpu,memory.used,memory.total "
                        "--format=csv,noheader", why="live GPU check")
    assert r["rc"] == 0, r
    assert "weftslurm" in r["stdout"]
    assert "MiB" in r["stdout"]            # the node's GPU telemetry
    tail = w.store.audit_tail(10)
    assert any(a["action"] == "job.node_exec" for a in tail)
    w.task_cancel(job)


def test_node_exec_guards(w):
    job = w.task_submit({"command": "sleep 20",
                         "resources": {"cpus": 1}, "site": "hpc"})["job_id"]
    deadline = time.time() + 90
    while time.time() < deadline:
        if w.task_status(job)[0]["state"] == "RUNNING":
            break
        time.sleep(0.5)
    denied = w.job_node_exec(job, "rm -rf /home", why="oops")
    assert denied.get("error") == "task.invalid"
    w.task_cancel(job)
    w.runner.wait(job, 120)
    late = w.job_node_exec(job, "hostname", why="too late")
    assert late.get("error") == "task.invalid"
    assert "RUNNING" in late["detail"]
