"""Controller ON the submit node (user-model ask): SlurmAdapter with
transport="local" — sbatch/squeue as direct subprocesses, no ssh-to-self
(GSSAPI-only sites refuse that hop outright). weft runs INSIDE the slurm
fixture container: the reporter's exact topology."""

import subprocess
import textwrap

import pytest

pytestmark = pytest.mark.docker

DRIVER = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, "/opt/weft-src")
    from weft.api import Weft

    w = Weft("/home/physicist/ws-local")
    w.register_site("here", "slurm", {
        # NO host: the controller IS the submit node -> local transport
        "root": "/home/physicist/.weft-local",
        "modules_init": "export MODULEPATH=/opt/site-modules",
    })
    w.runner.poll_interval = 0.3
    caps = w.sites_describe("here")["capabilities"]
    assert caps["scheduler"]["type"] == "slurm", caps.get("scheduler")

    r = w.task_submit({
        "command": "hostname > results/where.txt",
        "outputs": ["results/"],
        "resources": {"cpus": 1, "walltime": "00:05:00"},
        "site": "here"})
    assert "job_id" in r, r
    assert r["plan"]["queue"] == "slurm"
    job = w.runner.wait(r["job_id"], 120)
    assert job["state"] == "DONE", job.get("error")
    assert job["sched_handle"].startswith("slurm:")
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/where.txt")
    pl = w.provenance(r["job_id"])["placement"]
    print(json.dumps({"ok": True, "node": job["manifest"]["node"],
                      "hostname": out["preview"]["lines"][0],
                      "allocation": pl["allocation_id"]}))
""")


def _exec(container, *argv, input=None):
    return subprocess.run(["docker", "exec", "-i", container, *argv],
                          capture_output=True, text=True, input=input,
                          timeout=300)


def test_submit_node_controller_no_ssh(slurm_site):
    c = slurm_site["container"]
    r = subprocess.run(["docker", "cp", "src/weft",
                        f"{c}:/opt/weft-src/weft"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    # the topology premise: ssh-to-self is NOT available in-container
    # (no client keys) — only the scheduler is
    r = _exec(c, "su", "-", "physicist", "-c",
              "command -v sbatch && python3 /dev/stdin",
              input=DRIVER)
    assert r.returncode == 0, (r.stdout[-800:], r.stderr[-800:])
    assert '"ok": true' in r.stdout
    assert '"node": "weftslurm"' in r.stdout      # captured ON the node
    assert '"allocation": "slurm:' in r.stdout
