"""Persistent kernels: incremental execution with state, and every
operability lever — async blocks, interrupt, death diagnosis, replay."""

import subprocess
import time

import pytest

from weft.api import Weft


@pytest.fixture
def wk(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.3
    return w


def test_state_persists_across_blocks(wk):
    k = wk.kernel_start("local", "python")["kernel_id"]
    r1 = wk.kernel_exec(k, "x = 41")
    assert r1["rc"] == 0
    r2 = wk.kernel_exec(k, "x += 1\nprint(x)")
    assert r2["rc"] == 0 and r2["out"].strip() == "42"
    # a failed block reports, state survives
    r3 = wk.kernel_exec(k, "raise ValueError('detector misaligned')")
    assert r3["rc"] == 1 and "detector misaligned" in r3["err"]
    r4 = wk.kernel_exec(k, "print(x)")
    assert r4["out"].strip() == "42"
    st = wk.kernel_status(k)
    assert st["state"] == "running" and st["blocks_run"] == 4
    tr = wk.kernel_transcript(k)
    assert [e["rc"] for e in tr] == [0, 0, 1, 0]
    wk.kernel_stop(k)


def test_async_block_and_artifacts(wk):
    k = wk.kernel_start("local", "python")["kernel_id"]
    r = wk.kernel_exec(k, "import time, os\n"
                          "time.sleep(3)\n"
                          "open(os.environ['WEFT_BLOCK_DIR'] + '/fit.txt', 'w')"
                          ".write('done')", wait=False)
    assert r["state"] == "submitted"
    mid = wk.kernel_poll(k, r["block"], timeout=0.5)
    assert mid["state"] == "running"          # long block, watchable
    fin = wk.kernel_poll(k, r["block"], timeout=30)
    assert fin["state"] == "done" and fin["rc"] == 0
    assert "fit.txt" in fin["artifacts"]
    wk.kernel_stop(k)


def test_interrupt_hung_block(wk):
    k = wk.kernel_start("local", "python")["kernel_id"]
    wk.kernel_exec(k, "y = 7")
    r = wk.kernel_exec(k, "import time\ntime.sleep(600)", wait=False)
    time.sleep(1.5)
    wk.kernel_interrupt(k)
    fin = wk.kernel_poll(k, r["block"], timeout=20)
    assert fin["state"] == "done" and fin["rc"] == 130
    # interpreter survived the interrupt with state intact
    r2 = wk.kernel_exec(k, "print(y)")
    assert r2["out"].strip() == "7"
    wk.kernel_stop(k)


def test_death_is_diagnosed_and_replay_recovers(wk):
    k = wk.kernel_start("local", "python")["kernel_id"]
    wk.kernel_exec(k, "state = {'grid': list(range(10))}")
    wk.kernel_exec(k, "total = sum(state['grid'])")
    # a segfault-equivalent: kills the interpreter mid-block
    r = wk.kernel_exec(k, "import os\nos._exit(9)", wait=False)
    died = None
    for _ in range(100):
        events = wk.events_poll(0, 500)["events"]
        died = next((e for e in events if e["kind"] == "kernel.died"
                     and e["kernel"] == k), None)
        if died:
            break
        time.sleep(0.3)
    assert died, "poller must notice and report the death"
    assert died["killing_block"] == r["block"]  # THE diagnostic
    assert "kernel_restart" in died["suggestion"]
    assert wk.kernel_status(k)["state"] == "died"
    # exec on a dead kernel: structured, with the workaround named
    dead = wk.kernel_exec(k, "1+1")
    assert dead["error"] == "sched.node_failure"

    fresh = wk.kernel_restart(k, replay="successful")
    assert fresh["replayed_blocks"] == 2
    r2 = wk.kernel_exec(fresh["kernel_id"], "print(total)")
    assert r2["out"].strip() == "45"           # state rebuilt
    wk.kernel_stop(fresh["kernel_id"])


@pytest.mark.docker
def test_kernel_survives_disconnect(tmp_path, pixi_bin, sshd_site):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("beamlab", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin})
    w.adapters["beamlab"].poll_timeout = 2.0
    k = w.kernel_start("beamlab", "python")["kernel_id"]
    w.kernel_exec(k, "acc = 123")
    subprocess.run(["docker", "pause", sshd_site["container"]], check=True,
                   capture_output=True)
    w.adapters["beamlab"].close_control()
    time.sleep(5)
    subprocess.run(["docker", "unpause", sshd_site["container"]], check=True,
                   capture_output=True)
    r = w.kernel_exec(k, "print(acc)", timeout=60)
    assert r["rc"] == 0 and r["out"].strip() == "123"
    assert w.kernel_status(k)["state"] == "running"
    w.kernel_stop(k)


@pytest.mark.solver
@pytest.mark.slow
def test_r_kernel_with_env(wk):
    env = wk.env_ensure({"name": "r-kern", "deps": {"conda": ["r-base =4.4"]}})
    # realize the env by running a trivial task first
    r0 = wk.task_submit({"command": "true", "env": env["env_id"], "site": "local"})
    assert wk.runner.wait(r0["job_id"], 900)["state"] == "DONE"

    k = wk.kernel_start("local", "r", env_id=env["env_id"])["kernel_id"]
    assert wk.kernel_exec(k, "x <- 40", timeout=60)["rc"] == 0
    r = wk.kernel_exec(k, "x <- x + 2\ncat(x)", timeout=60)
    assert r["rc"] == 0 and r["out"].strip() == "42"
    bad = wk.kernel_exec(k, "stop('bad fit')", timeout=60)
    assert bad["rc"] == 1 and "bad fit" in bad["err"]
    assert wk.kernel_exec(k, "cat(x)", timeout=60)["out"].strip() == "42"

    # statement-level streaming: .out grows BETWEEN top-level expressions
    # while the block runs (base-R limit: within one expression it
    # arrives when the expression completes)
    import time as _t
    adapter = wk.adapters["local"]
    jobdir = wk.store.get_kernel(k)["jobdir"]
    r = wk.kernel_exec(
        k, "cat('a\\n')\nSys.sleep(2)\ncat('b\\n')\nSys.sleep(2)\n"
           "cat('c\\n')", wait=False)
    n = r["block"]
    partial = False
    for _ in range(60):
        _t.sleep(0.15)
        if adapter.file_exists(f"{jobdir}/blocks/{n:04d}.rc"):
            break
        try:
            body = adapter.read_file(f"{jobdir}/blocks/{n:04d}.out").decode()
        except Exception:
            continue
        if 0 < len(body.split()) < 3:
            partial = True
    assert wk.kernel_poll(k, n, timeout=30).get("rc") == 0
    assert partial, "R output never appeared between statements"
    wk.kernel_stop(k)
