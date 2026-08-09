"""Outage chaos on real sshd (docker lane): the embedder-truth doctrine
end-to-end — a transport outage spanning several poll ticks must never
mint a job verdict; the job survives to DONE and the event trail shows
exactly the outage window (aba live find, 2026-08-09: an sshd cut during
a running job became FAILED(site.unreachable) while the node computed
the true answer)."""

import subprocess
import time

import pytest

from weft.api import Weft

pytestmark = pytest.mark.docker


def _events(w, kind):
    return [e for e in w.store.events_since(0, 2000) if e["kind"] == kind]


def test_sshd_outage_midjob_yields_done_not_failed(tmp_path, pixi_bin,
                                                   sshd_site):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("box", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin,
        "control_persist": 5,
    })
    w.runner.poll_interval = 0.5
    jid = w.task_submit({"command": "sleep 4 && echo ok > out.txt",
                         "outputs": ["out.txt"], "site": "box"})["job_id"]
    deadline = time.time() + 120
    while time.time() < deadline:
        if w.store.get_job(jid)["state"] == "RUNNING":
            break
        time.sleep(0.2)
    assert w.store.get_job(jid)["state"] == "RUNNING"

    # freeze the whole box LONGER than poll_timeout (20s): the hung poll
    # must classify as an outage, not merely run slow — this is what
    # discriminates "doctrine held" from "outage never registered"
    subprocess.run(["docker", "pause", sshd_site["container"]], check=True,
                   capture_output=True)
    time.sleep(25)
    mid = w.store.get_job(jid)["state"]
    subprocess.run(["docker", "unpause", sshd_site["container"]], check=True,
                   capture_output=True)

    assert mid not in ("FAILED", "CANCELLED"), \
        f"outage minted a terminal verdict: {mid}"
    deadline = time.time() + 180
    while time.time() < deadline:
        row = w.store.get_job(jid)
        if row["state"] in ("DONE", "FAILED", "CANCELLED"):
            break
        time.sleep(0.5)
    assert row["state"] == "DONE", (row["state"], row.get("error"))
    assert not _events(w, "job.failed")
    assert _events(w, "site.unreachable"), "the outage must be an event"
    reach = _events(w, "site.reachable")
    assert reach and reach[-1]["outage_s"] > 0
