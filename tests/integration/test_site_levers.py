"""The agent-adjustability levers (CLAUDE.md principle #2): site_prelude
and scheduler directives — quirk fixes without weft code changes."""

import pytest

from weft.api import Weft


def test_site_prelude_runs_before_activation(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {
        "root": str(tmp_path / "site"), "pixi_source": pixi_bin,
        "site_prelude": "export WEFT_PRELUDE_PROOF=42"})
    w.runner.poll_interval = 0.2
    r = w.task_submit({"command": "echo $WEFT_PRELUDE_PROOF > results/o.txt",
                       "outputs": ["results/"], "site": "local"})
    job = w.runner.wait(r["job_id"], 60)
    assert job["state"] == "DONE", job["error"]
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/o.txt")
    assert out["preview"]["lines"] == ["42"]

    # kernels get the same prelude
    k = w.kernel_start("local", "python")["kernel_id"]
    try:
        rr = w.kernel_exec(k, "import os; print(os.environ.get("
                              "'WEFT_PRELUDE_PROOF'))", timeout=60)
        assert rr["rc"] == 0 and rr["out"].strip() == "42", rr
    finally:
        w.kernel_stop(k)


@pytest.mark.docker
def test_scheduler_directives_end_to_end(tmp_path, pixi_bin, slurm_site):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("hpc", "slurm", {
        "host": slurm_site["host"], "port": slurm_site["port"],
        "user": slurm_site["user"], "ssh_opts": slurm_site["ssh_opts"],
        "root": slurm_site["root"], "pixi_source": pixi_bin,
        "scheduler": {"extra_directives": ["--comment=weft-site-lever"]},
        "site_prelude": "export WEFT_PRELUDE_PROOF=slurm"})
    w.runner.poll_interval = 0.4

    r = w.task_submit({
        "command": "echo $WEFT_PRELUDE_PROOF > results/o.txt",
        "outputs": ["results/"],
        "resources": {"cpus": 1, "walltime": "00:05:00",
                      "scheduler_directives": ["--nice=10"]},
        "site": "hpc", "label": "lever proof"})
    job = w.runner.wait(r["job_id"], 600)
    assert job["state"] == "DONE", job["error"]
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/o.txt")
    assert out["preview"]["lines"] == ["slurm"]
    # both directive layers landed in the generated script
    batch = w.adapters["hpc"].read_file(
        f"jobs/{r['job_id']}/batch.sh").decode()
    assert "#SBATCH --comment=weft-site-lever" in batch
    assert "#SBATCH --nice=10" in batch

    # refusals: managed flag per task; dangerous flag site-level
    bad = w.task_submit({"command": "true", "site": "hpc",
                         "resources": {"scheduler_directives":
                                       ["--partition=short"]}})
    assert bad["error"] == "task.invalid"
    assert bad["hints"]["use_instead"] == "resources.partition"
    bad2 = w.register_site("hpc2", "slurm", {
        "host": slurm_site["host"], "port": slurm_site["port"],
        "user": slurm_site["user"], "ssh_opts": slurm_site["ssh_opts"],
        "root": slurm_site["root"] + "-2", "pixi_source": pixi_bin,
        "scheduler": {"extra_directives": ["--uid=0"]}})
    assert bad2["error"] == "task.invalid", bad2
