"""Chaos cases are the actual product surface (doc 06 §2).

Covered here: connectivity loss mid-job (docker pause), controller crash +
reconcile from remote state, scratch purge between staging and reuse, and
the session-env lifecycle.
"""

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from weft.api import Weft

pytestmark = [pytest.mark.docker, pytest.mark.slow]


def _mk_weft(tmp_path, pixi_bin, sshd_site, site_name="beamlab"):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site(site_name, "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin,
    })
    return w


def test_connectivity_loss_mid_job(tmp_path, pixi_bin, sshd_site):
    """Detached jobs survive outages; polling backs off and recovers."""
    w = _mk_weft(tmp_path, pixi_bin, sshd_site)
    w.runner.poll_interval = 0.3
    w.adapters["beamlab"].poll_timeout = 2.0   # surface the outage fast
    r = w.task_submit({
        "command": "sleep 10; echo survived > results/out.txt",
        "outputs": ["results/"], "site": "beamlab",
    })
    assert "job_id" in r, r
    # wait until RUNNING, then freeze the "workstation"
    for _ in range(100):
        if w.task_status(r["job_id"])[0]["state"] == "RUNNING":
            break
        time.sleep(0.1)
    subprocess.run(["docker", "pause", sshd_site["container"]], check=True,
                   capture_output=True)
    w.adapters["beamlab"].close_control()
    time.sleep(6)   # long enough that at least one poll times out
    subprocess.run(["docker", "unpause", sshd_site["container"]], check=True,
                   capture_output=True)

    job = w.runner.wait(r["job_id"], 180)
    assert job["state"] == "DONE", job["error"]
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/out.txt")
    assert out["preview"]["lines"] == ["survived"]
    kinds = [e["kind"] for e in w.events_poll(0, 500)["events"]]
    assert "site.unreachable" in kinds and "site.reachable" in kinds


CRASH_SCRIPT = textwrap.dedent("""\
    import json, os, sys, time
    sys.path.insert(0, {src!r})
    from weft.api import Weft
    cfg = json.loads(sys.argv[1])
    w = Weft(cfg["workspace"], pixi_bin=cfg["pixi_bin"])
    r = w.task_submit({{
        "command": "sleep 4; echo after-crash > results/out.txt",
        "outputs": ["results/"], "site": "beamlab",
    }})
    assert "job_id" in r, r
    print("DBG submit:", json.dumps(r)[:300], file=sys.stderr, flush=True)
    for _ in range(200):
        st = w.task_status(r["job_id"])
        print("DBG status:", st, file=sys.stderr, flush=True)
        if st and st[0]["state"] == "RUNNING":
            break
        time.sleep(0.1)
    print(r["job_id"], flush=True)
    os._exit(9)   # simulated controller crash: no cleanup, no atexit
""")


def test_controller_crash_and_reconcile(tmp_path, pixi_bin, sshd_site):
    """Kill the controller mid-job; a fresh controller reconciles from
    remote state and finishes collection (doc 01 §6)."""
    ws = tmp_path / "ws"
    # first controller: bootstrap + register in a subprocess, then die
    w0 = _mk_weft(tmp_path, pixi_bin, sshd_site)
    del w0
    src = str(Path(__file__).resolve().parents[2] / "src")
    cfg = json.dumps({"workspace": str(ws), "pixi_bin": pixi_bin})
    proc = subprocess.run(
        [sys.executable, "-c", CRASH_SCRIPT.format(src=src), cfg],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 9, proc.stderr
    job_id = proc.stdout.strip().splitlines()[-1]

    # second controller: same workspace, fresh process
    w = Weft(ws, pixi_bin=pixi_bin)
    stale = w.doctor()
    assert any(j["job_id"] == job_id for j in stale["nonterminal_jobs"])
    actions = w.reconcile()
    assert any(a["job"] == job_id for a in actions), actions
    job = w.runner.wait(job_id, 120)
    assert job["state"] == "DONE", job["error"]
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/out.txt")
    assert out["preview"]["lines"] == ["after-crash"]


def test_scratch_purge_demotes_and_recovers(tmp_path, pixi_bin, sshd_site):
    """Site-side deletion costs a re-transfer, never a wrong result
    (doc 04 §3) — and the structured error tells the agent exactly what
    to do; the retry succeeds."""
    w = _mk_weft(tmp_path, pixi_bin, sshd_site)
    data = tmp_path / "ws" / "run.dat"
    data.write_bytes(os.urandom(256 * 1024))
    ref = w.data_register("run.dat")["ref"]
    task = {
        "command": "sha256sum data/run.dat | cut -c1-16 > results/digest.txt",
        "inputs": [{"ref": ref, "mount_as": "data/run.dat"}],
        "outputs": ["results/"], "site": "beamlab",
    }
    r1 = w.task_submit(task)
    assert w.runner.wait(r1["job_id"], 120)["state"] == "DONE"

    # a site admin purges scratch behind our back
    w.adapters["beamlab"].run_cmd("rm -rf $WEFT_ROOT/cas/*")

    # location table still claims presence: plan says 0 bytes...
    r2 = w.task_submit(task, force=True)
    assert r2["plan"]["staging"]["bytes_to_move"] == 0
    job2 = w.runner.wait(r2["job_id"], 120)
    # ...so materialization fails, demotes locations, and says "resubmit"
    assert job2["state"] == "FAILED"
    err = job2["error"]
    assert err["error"] == "data.verify_failed" and err["retryable"] is True
    assert "resubmit" in err["hints"]["suggestion"]

    # the agent follows the hint: this time the plan re-transfers and it works
    r3 = w.task_submit(task, force=True)
    assert r3["plan"]["staging"]["bytes_to_move"] > 0
    job3 = w.runner.wait(r3["job_id"], 120)
    assert job3["state"] == "DONE", job3["error"]


@pytest.mark.solver
def test_realization_purge_rebuilds(tmp_path, pixi_bin, sshd_site):
    """Purged env realization: marker gone → demote → rebuild on next task."""
    w = _mk_weft(tmp_path, pixi_bin, sshd_site)
    env_id = w.env_ensure({"name": "tiny", "deps": {"conda": ["xz >=5"]}})["env_id"]
    t = {"command": "xz --version > results/v.txt", "env": env_id,
         "outputs": ["results/"], "site": "beamlab"}
    r1 = w.task_submit(t)
    assert w.runner.wait(r1["job_id"], 600)["state"] == "DONE"
    w.adapters["beamlab"].run_cmd("rm -rf $WEFT_ROOT/envs/*")
    r2 = w.task_submit(t, force=True)
    assert r2["plan"]["env"]["action"] == "cached"  # store believes it's there
    job2 = w.runner.wait(r2["job_id"], 600)
    assert job2["state"] == "DONE", job2["error"]  # rebuilt transparently


@pytest.mark.solver
def test_session_env_lifecycle(tmp_path, pixi_bin, sshd_site):
    """Interactive mode: mutate a scratch clone, snapshot to a real EnvID."""
    w = _mk_weft(tmp_path, pixi_bin, sshd_site)
    base = w.env_ensure({"name": "sess-base", "deps": {"conda": ["python =3.12"]}})
    env_id = base["env_id"]
    # realize the base on the site first
    r = w.task_submit({"command": "python -V > results/v.txt", "env": env_id,
                       "outputs": ["results/"], "site": "beamlab"})
    assert w.runner.wait(r["job_id"], 900)["state"] == "DONE"

    s = w.session_start(env_id, "beamlab")
    assert "session_id" in s, s
    sid = s["session_id"]
    # the exploratory loop: probe, discover missing dep, add it, retry
    probe = w.session_exec(sid, "python -c 'import requests' 2>&1; true")
    assert "ModuleNotFoundError" in probe["stdout"] + probe["stderr"]
    inst = w.session_install(sid, conda=["requests"])
    assert "installed" in inst, inst
    retry = w.session_exec(sid, "python -c 'import requests; print(requests.__name__)'")
    assert retry["rc"] == 0 and "requests" in retry["stdout"]

    # snapshot: minimal delta over the base, properly re-solved
    snap = w.session_snapshot(sid, name="with-requests")
    assert "env_id" in snap, snap
    assert snap["env_id"] != env_id
    assert snap["spec"]["deps"]["conda"] == ["requests"]
    assert snap["spec"]["extends"]  # pinned to the base spec hash

    # the snapshot env is a first-class citable env; run the "real" task in it
    r2 = w.task_submit({
        "command": "python -c 'import requests; print(\"ok\")' > results/ok.txt",
        "env": snap["env_id"], "outputs": ["results/"], "site": "beamlab",
    })
    job2 = w.runner.wait(r2["job_id"], 900)
    assert job2["state"] == "DONE", job2["error"]
    assert job2["manifest"]["env_id"] == snap["env_id"]  # provenance records it
    w.session_stop(sid)
