"""Retention against REMOTE compute (docker): the full lifecycle on an
ssh site with declared long-term storage, and a slurm-site smoke — the
paths the local tests can't vouch for."""

import json
import subprocess
import time
from pathlib import Path

import pytest

from weft.api import Weft

pytestmark = pytest.mark.docker


def test_full_lifecycle_on_remote_ssh_site(tmp_path, pixi_bin, sshd_site):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("beam", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin,
        "retain": {"dir": "/home/physicist/longterm"}})
    w.runner.poll_interval = 0.3

    r = w.task_submit({"command": "printf 'q,v\\n3,9\\n' > fit.csv; "
                                  "echo scratch > tmp.dat",
                       "site": "beam"})
    jid = r["job_id"]
    assert w.runner.wait(jid, 300)["state"] == "DONE"

    # terminal inventory recorded for the REMOTE run
    inv = w.run_inventory(jid)
    assert {"fit.csv", "tmp.dat"} <= {e["path"] for e in inv["entries"]}
    assert inv["site"] == "beam"

    # in-place retention on the site's declared storage
    kept = w.run_retain(jid, include=["fit.csv"], label="rem-1",
                        background=False)
    assert kept["state"] == "done" and kept["in_place"]
    on_site = f"{kept['location']['path']}/fit.csv"

    # re-entry: site-side registration, lineage preserved, 0-byte reuse
    reg = w.data_register(on_site, site="beam")
    assert w.provenance(reg["ref"])["origin"] == f"run:{jid}/fit.csv"
    rb = w.task_submit({"command": "cat in.csv > results/echo.csv",
                        "inputs": [{"ref": reg["ref"],
                                    "mount_as": "in.csv"}],
                        "outputs": ["results/"], "site": "beam"})
    assert rb["plan"]["staging"]["bytes_to_move"] == 0
    job = w.runner.wait(rb["job_id"], 300)
    assert job["state"] == "DONE"
    # provenance of the downstream run walks through to the producer
    p = w.provenance(rb["job_id"])
    assert p["inputs"][0]["produced_by"]["job_id"] == jid

    # discard the sandbox; retained + registered survive; forget reclaims
    w.run_discard(jid)
    a = w.adapters["beam"]
    assert a.run_cmd(f"cat {on_site}").out.startswith("q,v")
    out = w.run_forget(label="rem-1")
    assert out["forgotten"][0]["target"] == jid
    assert a.run_cmd(f"ls {kept['location']['path']} 2>/dev/null").out == ""
    assert w.run_inventory(jid)["entries"]        # knowledge survives


def test_remote_kernel_capture_and_late_promote(tmp_path, pixi_bin,
                                                sshd_site):
    """Remote kernel: transcript mirrored as observed; text + replay
    survive the remote sandbox being purged."""
    w = Weft(tmp_path / "ws-k", pixi_bin=pixi_bin)
    w.register_site("beam", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.3
    k = w.kernel_start("beam", "python")["kernel_id"]
    assert w.kernel_exec(k, "y = 6", timeout=120)["rc"] == 0
    r = w.kernel_exec(k, "print(y * 7)", timeout=120)
    assert r["out"].strip() == "42"
    w.kernel_stop(k)
    jobdir = w.store.get_kernel(k)["jobdir"]
    w.adapters["beam"].run_cmd(
        f"rm -rf {w.adapters['beam'].path(jobdir)}")   # scratch purge
    t = w.kernel_transcript(k)
    assert "42" in t[1]["out_tail"]                    # mirror held
    fresh = w.kernel_restart(k)
    assert fresh["replayed_blocks"] == 2
    w.kernel_stop(fresh["kernel_id"])


def test_mid_transfer_failure_is_honest_and_resumable(tmp_path, pixi_bin,
                                                      sshd_site):
    """Kill the wire DURING the tar-pipe: state lands failed+retryable
    (no lying 'done'), forget-during-inflight is refused, and a retry
    after recovery converges to correct content."""
    w = Weft(tmp_path / "ws-c", pixi_bin=pixi_bin)
    w.register_site("beam", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.3
    r = w.task_submit({"command": "dd if=/dev/urandom of=big.bin "
                                  "bs=1M count=48 2>/dev/null; "
                                  "sha256sum big.bin > big.sha",
                       "site": "beam"})
    jid = r["job_id"]
    assert w.runner.wait(jid, 300)["state"] == "DONE"

    out = w.run_retain(jid, include=["big.bin", "big.sha"],
                       background=True)
    # freeze the container while bytes are in flight
    for _ in range(100):
        row = w.store.get_retained(jid)
        if row and row["state"] == "inflight":
            break
        time.sleep(0.05)
    subprocess.run(["docker", "pause", sshd_site["container"]], check=True)
    try:
        fr = w.run_forget(target=jid)         # racing forget refused
        assert fr["forgotten"] == []
        assert fr["forget_pending"][0]["why"] == "retain.in_flight"
        for _ in range(240):                  # ssh keepalive kills ~30s
            if w.store.get_retained(jid)["state"] == "failed":
                break
            time.sleep(0.5)
        assert w.store.get_retained(jid)["state"] == "failed"
    finally:
        subprocess.run(["docker", "unpause", sshd_site["container"]],
                       check=True)

    retry = w.run_retain(jid, include=["big.bin", "big.sha"],
                         background=False)     # retry converges
    assert retry["state"] == "done"
    dest = Path(retry["location"]["path"])
    import hashlib
    got = hashlib.sha256((dest / "big.bin").read_bytes()).hexdigest()
    assert got == (dest / "big.sha").read_text().split()[0]


def test_remote_live_pin_settles_and_transfers(tmp_path, pixi_bin,
                                               sshd_site):
    """Pin on a LIVE remote kernel: recorded now, captured at stop,
    transferred home byte-equal."""
    w = Weft(tmp_path / "ws-p", pixi_bin=pixi_bin)
    w.register_site("beam", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.3
    k = w.kernel_start("beam", "python")["kernel_id"]
    assert w.kernel_exec(k, "open('result.bin','wb')"
                            ".write(bytes(range(256))*100)",
                         timeout=120)["rc"] == 0
    pin = w.run_retain(k, include=["result.bin"], label="remote-pin")
    assert pin["state"] == "pinned-pending"
    w.kernel_stop(k)
    for _ in range(100):                        # transfer is async-ish
        row = w.retained_runs(label="remote-pin")[0]
        if row["state"] in ("done", "failed"):
            break
        time.sleep(0.3)
    assert row["state"] == "done", row
    body = (Path(row["location"]) / "result.bin").read_bytes()
    assert body == bytes(range(256)) * 100


def test_slurm_site_retention_smoke(tmp_path, pixi_bin, slurm_site):
    """Scheduler site: inventory + retain-home + forget, through sbatch."""
    w = Weft(tmp_path / "ws-s", pixi_bin=pixi_bin)
    w.register_site("hpc", "slurm", {
        "host": slurm_site["host"], "port": slurm_site["port"],
        "user": slurm_site["user"], "ssh_opts": slurm_site["ssh_opts"],
        "root": slurm_site["root"], "pixi_source": pixi_bin,
        "modules_init": slurm_site["modules_init"]})
    w.runner.poll_interval = 0.3
    r = w.task_submit({"command": "echo scan-done > point.txt",
                       "resources": {"cpus": 1, "walltime": "00:05:00"},
                       "site": "hpc"})
    jid = r["job_id"]
    assert w.runner.wait(jid, 240)["state"] == "DONE"
    assert "point.txt" in {e["path"] for e in
                           w.run_inventory(jid)["entries"]}
    kept = w.run_retain(jid, include=["point.txt"], background=False)
    assert kept["state"] == "done"
    dest = Path(kept["location"]["path"])
    assert (dest / "point.txt").read_text() == "scan-done\n"
    sidecar = json.loads((dest / ".weft-run.json").read_text())
    assert sidecar["site"] == "hpc" and sidecar["node"] == "weftslurm"
    w.run_forget(target=jid)
    assert not dest.exists()
