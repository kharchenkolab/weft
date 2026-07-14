"""Scenarios S2/S4 against a real (containerized) Slurm scheduler:
queue lifecycle, arrays, partitions, modules, packed realization for
air-gapped compute, and scheduler chaos (rejection, walltime, cancel)."""

import time

import pytest

from weft.api import Weft

pytestmark = pytest.mark.docker


@pytest.fixture
def weft_slurm(tmp_path, pixi_bin, slurm_site):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("hpc", "slurm", {
        "host": slurm_site["host"], "port": slurm_site["port"],
        "user": slurm_site["user"], "ssh_opts": slurm_site["ssh_opts"],
        "root": slurm_site["root"], "pixi_source": pixi_bin,
        "modules_init": slurm_site["modules_init"],
    })
    return w


def test_probe_sees_scheduler_and_partitions(weft_slurm):
    from weft.capability import slurm_time_to_s
    caps = weft_slurm.sites_describe("hpc")["capabilities"]
    assert caps["scheduler"]["type"] == "slurm"
    parts = {p["name"]: p for p in caps["scheduler"]["partitions"]}
    assert "standard" in parts and "short" in parts
    assert parts["standard"]["cpus_per_node"] == 8
    assert parts["standard"]["nodes"] == 1        # "how big is it" (%D)
    assert slurm_time_to_s(parts["short"]["max_walltime"]) == 60
    assert slurm_time_to_s(parts["standard"]["max_walltime"]) == 3600
    # storage candidates carry totals — utilization is computable, not
    # fabricated
    for c in caps["storage"]["candidates"]:
        assert c["total_gb"] >= c["free_gb"] >= 0


def test_batch_job_full_lifecycle(weft_slurm, tmp_path):
    grid = tmp_path / "ws" / "grid.csv"
    grid.write_text("point,mass\n" + "\n".join(f"{i},{80 + i * 0.05}" for i in range(200)))
    ref = weft_slurm.data_register("grid.csv")["ref"]
    weft_slurm.runner.poll_interval = 0.3
    r = weft_slurm.task_submit({
        "command": "sleep 3; "   # long enough for RUNNING to be observed
                   "awk -F, 'NR>1 {n++} END {print n}' data/grid.csv > results/points.txt",
        "inputs": [{"ref": ref, "mount_as": "data/grid.csv"}],
        "outputs": ["results/"],
        "resources": {"cpus": 2, "mem_gb": 1, "walltime": "00:10:00"},
        "site": "hpc",
    })
    assert "job_id" in r, r
    assert r["plan"]["queue"] == "slurm"
    job = weft_slurm.runner.wait(r["job_id"], 180)
    assert job["state"] == "DONE", job["error"]
    assert job["sched_handle"].startswith("slurm:")
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/points.txt")
    assert out["preview"]["lines"] == ["200"]
    # lifecycle went through the queue
    states = [e["state"] for e in weft_slurm.events_poll(0, 500)["events"]
              if e["kind"] == "job.state" and e.get("job_id") == r["job_id"]]
    assert "QUEUED" in states and "RUNNING" in states


def test_provenance_placement_through_scheduler(weft_slurm):
    """Placement resolves through the queue: the node file is written by
    the batch script on the EXECUTING node, the allocation is the slurm
    job id, and node_truth is labeled with the partition probe source."""
    weft_slurm.runner.poll_interval = 0.3
    r = weft_slurm.task_submit({
        "command": "true",
        "resources": {"cpus": 1, "walltime": "00:01:00",
                      "partition": "short"},
        "site": "hpc"})
    job = weft_slurm.runner.wait(r["job_id"], 180)
    assert job["state"] == "DONE", job["error"]
    pl = weft_slurm.provenance(r["job_id"])["placement"]
    assert pl["site"] == "hpc"
    assert pl["node"] == "weftslurm"           # the container's hostname
    assert pl["allocation_id"].startswith("slurm:")
    assert pl["partition"] == "short"
    if pl["node_truth"] is not None:
        assert "short" in pl["node_truth"]["source"] \
            or pl["node_truth"]["source"] == "site probe"


def test_array_scan_fanout(weft_slurm):
    import threading
    adapter = weft_slurm.adapters["hpc"]
    sbatch_calls = []
    orig = adapter.run_cmd
    lock = threading.Lock()

    def counting(script, **kw):
        if "sbatch" in script and "--test-only" not in script:
            with lock:
                sbatch_calls.append(script[:60])
        return orig(script, **kw)

    adapter.run_cmd = counting
    r = weft_slurm.task_submit({
        "command": "echo \"scan point $WEFT_ARRAY_INDEX done\" > results/point.txt",
        "outputs": ["results/"],
        "resources": {"cpus": 1, "walltime": "00:05:00"},
        "site": "hpc", "array": 4,
    })
    assert r["elements"] == 4, r
    assert r.get("native_array", "").startswith("slurm:")
    got = set()
    for sub in r["jobs"]:
        job = weft_slurm.runner.wait(sub["job_id"], 240)
        assert job["state"] == "DONE", job["error"]
        assert "_" in job["sched_handle"]   # element handle slurm:JID_i
        out = next(o for o in job["manifest"]["outputs"]
                   if o["path"] == "results/point.txt")
        got.add(out["preview"]["lines"][0])
    assert got == {f"scan point {i} done" for i in range(4)}
    # the login-node politeness contract: ONE submission for the whole scan
    assert len(sbatch_calls) == 1, sbatch_calls
    adapter.run_cmd = orig


def test_capability_violation_against_partitions(weft_slurm):
    r = weft_slurm.task_submit({
        "command": "true", "site": "hpc",
        "resources": {"cpus": 64},   # node has 8
    })
    assert r["error"] == "site.capability_violation", r
    assert "suggestion" in r["hints"]


def test_scheduler_walltime_kill(weft_slurm):
    """Job exceeds its --time: slurm kills it; weft reports the taxonomy
    code with the ask attached (slurm enforces in ~1min granularity)."""
    r = weft_slurm.task_submit({
        "command": "sleep 600",
        "resources": {"cpus": 1, "walltime": "00:01:00"},
        "site": "hpc",
    })
    assert "job_id" in r, r
    job = weft_slurm.runner.wait(r["job_id"], 300)
    assert job["state"] == "FAILED"
    assert job["error"]["error"] == "job.walltime_exceeded"
    assert job["error"]["hints"]["requested"] == "00:01:00"


def test_cancel_queued_or_running(weft_slurm):
    r = weft_slurm.task_submit({
        "command": "sleep 300", "resources": {"cpus": 1}, "site": "hpc",
    })
    assert "job_id" in r, r
    time.sleep(2)
    weft_slurm.task_cancel(r["job_id"])
    job = weft_slurm.runner.wait(r["job_id"], 60)
    assert job["state"] == "CANCELLED"


@pytest.mark.solver
def test_module_spec_realizes_and_loads(weft_slurm, linux_platforms):
    """S4: spec declares a site module; activation loads it; the task sees
    the module's environment (mock espresso/7.2 modulefile)."""
    chk = weft_slurm.module_check("hpc", ["espresso/7.2", "nonexistent/9.9"])
    assert chk["modules"]["espresso/7.2"] is True
    assert chk["modules"]["nonexistent/9.9"] is False

    env = weft_slurm.env_ensure({
        "name": "dft-post",
        "platforms": linux_platforms,
        "deps": {"conda": ["xz >=5"]},
        "modules": ["espresso/7.2"],
    })
    assert "env_id" in env, env
    r = weft_slurm.task_submit({
        "command": "echo \"root=$ESPRESSO_ROOT\" > results/mod.txt; "
                   "xz --version >> results/mod.txt",
        "env": env["env_id"],
        "outputs": ["results/"],
        "resources": {"cpus": 1, "walltime": "00:10:00"},
        "site": "hpc",
    })
    assert "job_id" in r, r
    job = weft_slurm.runner.wait(r["job_id"], 600)
    assert job["state"] == "DONE", job["error"]
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/mod.txt")
    assert out["preview"]["lines"][0] == "root=/opt/espresso-7.2"
    real = weft_slurm.env_status(env["env_id"])["realizations"]
    assert any(x["site"] == "hpc" and x["strategy"] == "modules+prefix"
               and x["state"] == "ready" for x in real)


@pytest.mark.solver
def test_missing_module_unsatisfiable_hint(weft_slurm, linux_platforms):
    env = weft_slurm.env_ensure({
        "name": "needs-missing-module",
        "platforms": linux_platforms,
        "deps": {"conda": ["xz >=5"]},
        "modules": ["cray-mpich/8.1"],   # not on this "cluster"
    })
    assert "env_id" in env, env
    r = weft_slurm.task_submit({
        "command": "true", "env": env["env_id"],
        "resources": {"cpus": 1}, "site": "hpc",
    })
    job = weft_slurm.runner.wait(r["job_id"], 300) if "job_id" in r else None
    # module load fails in activation -> realization or job failure with
    # the module named in the cause
    if job is not None:
        assert job["state"] == "FAILED"
        text = str(job["error"])
        assert "cray-mpich" in text
    else:
        assert "cray-mpich" in str(r)


@pytest.mark.solver
@pytest.mark.slow
def test_packed_realization_for_airgapped_compute(tmp_path, pixi_bin,
                                                  slurm_site, linux_platforms):
    """Compute nodes with no internet (simulated via capabilities_override):
    the strategy selector picks `packed`; pixi-pack builds locally, the
    archive rides the data plane, unpack happens site-side with no network."""
    w = Weft(tmp_path / "ws2", pixi_bin=pixi_bin)
    w.register_site("hpc-dark", "slurm", {
        "host": slurm_site["host"], "port": slurm_site["port"],
        "user": slurm_site["user"], "ssh_opts": slurm_site["ssh_opts"],
        "root": "/home/physicist/.weft-dark",
        "pixi_source": pixi_bin,
        "capabilities_override": {"internet": False,
                                  "runtimes": {"apptainer": "", "docker": False}},
    })
    env = w.env_ensure({"name": "packed-env", "platforms": linux_platforms,
                        "deps": {"conda": ["xz >=5"]}})
    assert "env_id" in env, env
    r = w.task_submit({
        "command": "xz --version > results/v.txt",
        "env": env["env_id"],
        "outputs": ["results/"],
        "resources": {"cpus": 1, "walltime": "00:10:00"},
        "site": "hpc-dark",
    })
    assert "job_id" in r, r
    job = w.runner.wait(r["job_id"], 600)
    assert job["state"] == "DONE", job["error"]
    real = w.env_status(env["env_id"])["realizations"]
    assert any(x["site"] == "hpc-dark" and x["strategy"] == "packed"
               and x["state"] == "ready" for x in real)
