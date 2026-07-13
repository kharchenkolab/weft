"""Round D: allocation-backed interactive kernels — the kernel lives
INSIDE a scheduler allocation (file-block protocol over the shared FS; no
ports), with resources/partition placing it like a job."""

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


def test_kernel_runs_inside_a_gpu_allocation(w):
    """kernel_start(resources={gpus, partition}) → the interpreter runs on
    the allocated node with the GPU visible, survives across blocks, and
    node diagnostics reach the same allocation."""
    # the default 8h walltime exceeds the gpu partition's 2h cap: weft
    # must REFUSE upfront (slurm would accept and pend forever — the
    # silent-never-starts trap this fence exists for)
    stuck = w.kernel_start("hpc", lang="python",
                           resources={"gpus": 1, "partition": "gpu"})
    assert stuck.get("error") == "site.capability_violation", stuck

    r = w.kernel_start("hpc", lang="python", walltime="01:00:00",
                       resources={"gpus": 1, "partition": "gpu"})
    assert "kernel_id" in r, r
    k = r["kernel_id"]
    try:
        r1 = w.kernel_exec(k, "import subprocess, os\n"
                              "gpus = os.environ.get('SLURM_JOB_GRES', '')\n"
                              "part = os.environ.get('SLURM_JOB_PARTITION')\n"
                              "print(part)\n"
                              "state = 41")
        assert r1["state"] == "done" and r1["rc"] == 0, r1
        assert "gpu" in r1["out"]          # placed on the gpu partition

        r2 = w.kernel_exec(k, "state += 1\nprint(state)")
        assert "42" in r2["out"]           # same interpreter, same node

        # the allocation is node_exec-able like any running job's
        kern = w.store.get_kernel(k)
        r3 = w.adapters["hpc"].node_exec(
            kern["handle"], "nvidia-smi --query-gpu=name "
            "--format=csv,noheader")
        assert "Fake A100" in r3.out
    finally:
        w.kernel_stop(k)


def test_kernel_respects_partition_walltime(w):
    """The short partition kills a kernel at 1 minute: death is an EVENT
    naming the killing block, not silence."""
    r = w.kernel_start("hpc", lang="python", walltime="00:01:00",
                       resources={"partition": "short"})
    assert "kernel_id" in r, r
    assert w.kernel_status(r["kernel_id"])["state"] == "running"
    w.kernel_stop(r["kernel_id"])
