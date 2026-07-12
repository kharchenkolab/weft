"""Login-node politeness at scale + remote hard-crash semantics."""

import subprocess
import threading
import time

import pytest

from weft.api import Weft

pytestmark = [pytest.mark.docker, pytest.mark.slow]


def test_polling_scales_with_ticks_not_jobs(tmp_path, pixi_bin, slurm_site):
    """N jobs must cost O(ticks) scheduler queries, not O(N × ticks)."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("hpc", "slurm", {
        "host": slurm_site["host"], "port": slurm_site["port"],
        "user": slurm_site["user"], "ssh_opts": slurm_site["ssh_opts"],
        "root": slurm_site["root"], "pixi_source": pixi_bin,
        "policy": {"poll_interval_s": 1.0},
    })
    adapter = w.adapters["hpc"]
    counts = {"squeue": 0, "shim_status": 0}
    lock = threading.Lock()
    orig_run = adapter.run_cmd
    orig_shim = adapter.shim

    def counting_run(script, **kw):
        if "squeue" in script:
            with lock:
                counts["squeue"] += 1
        return orig_run(script, **kw)

    def counting_shim(argv, **kw):
        if argv and argv[0] == "status":
            with lock:
                counts["shim_status"] += 1
        return orig_shim(argv, **kw)

    adapter.run_cmd = counting_run
    adapter.shim = counting_shim

    n = 12
    r = w.task_submit({
        "command": "sleep 4; echo $WEFT_ARRAY_INDEX > results/i.txt",
        "outputs": ["results/"],
        "resources": {"cpus": 1, "walltime": "00:05:00"},
        "site": "hpc", "array": n,
    })
    assert r["elements"] == n, r
    t0 = time.time()
    for sub in r["jobs"]:
        job = w.runner.wait(sub["job_id"], 300)
        assert job["state"] == "DONE", job["error"]
    elapsed = time.time() - t0

    # one squeue per tick (~1s), regardless of 12 jobs; generous ceiling
    # covers submission-phase ticks. The naive per-job model would need
    # n * ticks ≈ 12 * elapsed queries.
    ticks_upper = int(elapsed) + 15
    assert counts["squeue"] <= ticks_upper, (counts, elapsed)
    assert counts["squeue"] < n * max(int(elapsed), 1)
    # no per-job single status polls (batch path only)
    assert counts["shim_status"] == 0
    # thread bound: pollers ≤ sites, collectors ≤ 8, not O(jobs)
    assert threading.active_count() < 30


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_remote_hard_crash_yields_node_failure(tmp_path, pixi_bin, sshd_site):
    """The 'workstation rebooted' case: processes gone, pid files (without
    exit codes) survive, and — crucially — pids get recycled, so a naive
    kill -0 would report the dead job alive forever. The job must fail as
    sched.node_failure with the cause named.

    Uses a dedicated container: restarting the shared fixture would move
    its randomly-published port and sabotage neighbouring tests (a lesson
    the first version of this test taught the hard way).
    """
    import uuid
    port = _free_port()
    name = f"weft-crash-{uuid.uuid4().hex[:8]}"
    # an explicit -p mapping survives docker restart; a random one does not
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name,
         "-p", f"127.0.0.1:{port}:22", "weft-test-sshd"],
        check=True, capture_output=True,
    )
    try:
        for _ in range(60):
            ok = subprocess.run(
                ["ssh", *sshd_site["ssh_opts"], "-o", "BatchMode=yes",
                 "-p", str(port), "physicist@127.0.0.1", "true"],
                capture_output=True)
            if ok.returncode == 0:
                break
            time.sleep(0.5)
        w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
        w.register_site("beamlab", "ssh", {
            "host": "127.0.0.1", "port": port,
            "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
            "root": sshd_site["root"], "pixi_source": pixi_bin,
        })
        w.runner.poll_interval = 0.5
        r = w.task_submit({"command": "sleep 300", "site": "beamlab"})
        assert "job_id" in r, r
        for _ in range(100):
            if w.task_status(r["job_id"])[0]["state"] == "RUNNING":
                break
            time.sleep(0.1)
        # hard restart: kills every process; the filesystem (pid files
        # without exit codes) survives — exactly what a reboot leaves behind
        subprocess.run(["docker", "restart", "-t", "0", name],
                       check=True, capture_output=True)
        w.adapters["beamlab"].close_control()

        job = w.runner.wait(r["job_id"], 180)
        assert job["state"] == "FAILED"
        err = job["error"]
        assert err["error"] == "sched.node_failure"
        assert "crash or reboot" in err["detail"]
        # note: a site.unreachable event only appears if a poll landed
        # during the brief down window — with a -t 0 restart it may not,
        # and that's correct (outage events describe observed outages)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
