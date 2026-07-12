"""In-flight transfer progress: start/progress/done events with rates."""

import os

import pytest

from weft.api import Weft

pytestmark = pytest.mark.docker


def test_rsync_progress_events(tmp_path, pixi_bin, sshd_site):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("beamlab", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin,
    })
    # throttle so a loopback transfer lasts long enough to observe (~5s)
    w.transfers["rsync-ssh"].extra_args = ["--bwlimit=4000"]

    size = 20 * 1024 * 1024
    (tmp_path / "ws" / "frames.raw").write_bytes(os.urandom(size))
    ref = w.data_register("frames.raw")["ref"]
    r = w.task_submit({
        "command": "wc -c < data/frames.raw > results/n.txt",
        "inputs": [{"ref": ref, "mount_as": "data/frames.raw"}],
        "outputs": ["results/"], "site": "beamlab",
    })
    assert r["plan"]["staging"]["bytes_to_move"] == size
    assert "estimate_s" in r["plan"]["staging"]  # plan-time honesty
    job = w.runner.wait(r["job_id"], 300)
    assert job["state"] == "DONE", job["error"]

    events = w.events_poll(0, 500)["events"]
    starts = [e for e in events if e["kind"] == "transfer.start"]
    progresses = [e for e in events if e["kind"] == "transfer.progress"]
    dones = [e for e in events if e["kind"] == "transfer.done"]
    assert len(starts) == 1 and starts[0]["bytes_total"] == size
    assert starts[0]["method"] == "rsync-ssh"
    assert starts[0]["job_id"] == r["job_id"]
    assert len(progresses) >= 2, "throttled transfer must yield progress"
    done_bytes = [p["bytes_done"] for p in progresses]
    assert done_bytes == sorted(done_bytes)  # monotone
    assert all(p["bytes_total"] == size and "eta_s" in p for p in progresses)
    assert len(dones) == 1 and dones[0]["elapsed_s"] > 2
    assert dones[0]["rate_mbps"] > 0
