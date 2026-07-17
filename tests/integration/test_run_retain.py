"""Retention R2/R3 (misc/retention.md): plain-file retention — local
placement via the discovered mechanism chain, finished-things-only,
sidecar provenance, label grouping, queue survival across process
death."""

import json
import os
import time
from pathlib import Path

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def _run(w, cmd):
    r = w.task_submit({"command": cmd, "site": "local"})
    assert w.runner.wait(r["job_id"], 120)["state"] == "DONE"
    return r["job_id"]


def test_local_retain_places_and_records(w, tmp_path):
    jid = _run(w, "mkdir -p figs tmp && echo svg > figs/a.svg && "
                  "echo big > out.dat && echo junk > tmp/x")
    r = w.run_retain(jid, include=["figs/**", "out.dat"],
                     exclude=["tmp/**"], label="paper-1",
                     background=False, dest="@workspace")
    assert r["state"] == "done" and r["files"] == 2
    dest = Path(r["location"]["path"])
    assert (dest / "figs/a.svg").read_text() == "svg\n"
    assert (dest / "out.dat").exists()
    assert not (dest / "tmp").exists()
    # zero-copy where the fs allows: same inode (link) or clone —
    # content identity is the contract, method is reported
    row = w.retained_runs(label="paper-1")[0]
    assert row["target"] == jid and row["state"] == "done"
    assert row["method"] in ("reflink|link|copy", "transfer")
    sidecar = json.loads((dest / ".weft-run.json").read_text())
    assert sidecar["target"] == jid and sidecar["site"] == "local"
    assert {f["path"] for f in sidecar["files"]} == {"figs/a.svg",
                                                     "out.dat"}


def test_retain_on_running_job_pins_and_overcap_refused(w, tmp_path):
    r = w.task_submit({"command": "sleep 30", "site": "local"})
    time.sleep(1)
    # a live-run retain is a PIN now (pin-at-settlement addendum)
    out = w.run_retain(r["job_id"], background=False, dest="@workspace")
    assert out["state"] == "pinned-pending"
    w.run_forget(target=r["job_id"])       # cancel the pin
    w.task_cancel(r["job_id"])

    jid = _run(w, "dd if=/dev/zero of=big.bin bs=1024 count=64 2>/dev/null")
    out = w.run_retain(jid, include=["big.bin"], max_gb=0.00001,
                       background=False, dest="@workspace")
    assert out["error"] == "task.invalid" and "GB" in out["detail"]


def test_live_kernel_retains_completed_blocks_only(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    r = w.kernel_exec(
        k, "import os\n"
           "open(os.environ['WEFT_BLOCK_DIR'] + '/t.csv', 'w')"
           ".write('a,b\\n1,2')\n"
           "open('loose.txt', 'w').write('cwd file')", timeout=60)
    assert r["rc"] == 0
    # completed block's artifacts: retainable while the kernel lives
    out = w.run_retain(k, include=["blocks/*.artifacts/**"],
                       background=False, dest="@workspace")
    assert out["state"] == "done"
    dest = Path(out["location"]["path"])
    assert (dest / f"blocks/{r['block']:04d}.artifacts/t.csv").exists()
    # a loose cwd file mid-life becomes a PIN, captured at settlement
    pin = w.run_retain(k, include=["loose.txt"], background=False, dest="@workspace")
    assert pin["state"] == "pinned-pending"
    w.kernel_stop(k)
    row = w.store.get_retained(k)
    assert row["state"] == "done"          # settled at stop
    assert (Path(row["location"]) / "loose.txt").read_text() == "cwd file"


def test_background_retain_survives_process_death(w, tmp_path, pixi_bin):
    jid = _run(w, "echo keep > result.txt")
    # enqueue, then the process "dies" before the thread can be trusted
    w.run_retain(jid, include=["result.txt"], background=True, dest="@workspace")
    del w

    w2 = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w2.reconcile()
    for _ in range(50):
        rows = w2.retained_runs()
        if rows and rows[0]["state"] == "done":
            break
        time.sleep(0.2)
    assert rows[0]["state"] == "done"
    assert (Path(rows[0]["location"]) / "result.txt").exists()


@pytest.mark.docker
def test_remote_retain_transfers_home(tmp_path, pixi_bin, sshd_site):
    """No retain.dir on the remote: retained files tar-pipe home to the
    workspace, byte-for-byte."""
    w = Weft(tmp_path / "ws-r", pixi_bin=pixi_bin)
    w.register_site("beam", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.3
    r = w.task_submit({"command": "mkdir -p figs && "
                                  "printf 'x,y\\n1,4\\n' > figs/t.csv && "
                                  "echo noise > junk.log",
                       "site": "beam"})
    assert w.runner.wait(r["job_id"], 300)["state"] == "DONE"
    out = w.run_retain(r["job_id"], include=["figs/**"],
                       background=False, dest="@workspace")
    assert out["state"] == "done", out
    assert out["location"]["site"] == "@workspace"
    dest = Path(out["location"]["path"])
    assert (dest / "figs/t.csv").read_text() == "x,y\n1,4\n"
    assert not (dest / "junk.log").exists()
    row = w.retained_runs()[0]
    assert row["method"] == "transfer" and row["in_place"] == 0


def test_retain_dir_site_keeps_files_in_place(tmp_path, pixi_bin):
    """A site with declared long-term storage: retained files land
    site-side under retain.dir; nothing lands in the workspace."""
    keep = tmp_path / "longterm"
    keep.mkdir()
    w = Weft(tmp_path / "ws2", pixi_bin=pixi_bin)
    w.register_site("local", "local", {
        "root": str(tmp_path / "site2"), "pixi_source": pixi_bin,
        "retain": {"dir": str(keep)}})
    w.runner.poll_interval = 0.2
    jid = _run(w, "echo stay > model.bin")
    r = w.run_retain(jid, include=["model.bin"], background=False)
    assert r["in_place"] is True
    assert r["location"]["path"].startswith(str(keep))
    assert (keep / "runs" / jid / "model.bin").read_text() == "stay\n"
    assert not (tmp_path / "ws2" / "runs").exists()
    sidecar = json.loads(
        (keep / "runs" / jid / ".weft-run.json").read_text())
    assert sidecar["target"] == jid
