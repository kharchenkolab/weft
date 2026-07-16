"""Retention R6 (misc/retention.md): capture="transcript" makes kernel
text durable as observed, and the late-save contracts hold — promote
after death, replay after the sandbox is gone."""

import subprocess
import time

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def test_transcript_survives_sandbox_deletion_and_replays(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    w.kernel_exec(k, "x = 40", timeout=60)
    r = w.kernel_exec(k, "x += 2\nprint(x)", timeout=60)
    assert r["rc"] == 0
    w.kernel_stop(k)

    # scratch purge: the sandbox vanishes entirely
    jobdir = w.store.get_kernel(k)["jobdir"]
    w.adapters["local"].run_cmd(
        f"rm -rf {w.adapters['local'].path(jobdir)}")

    # the durable mirror still tells the whole story
    t = w.kernel_transcript(k)
    assert [e["rc"] for e in t] == [0, 0]
    assert "x += 2" in t[1]["code"] and "42" in t[1]["out_tail"]

    # and replay rebuilds the state in a NEW kernel from that mirror
    fresh = w.kernel_restart(k)
    assert fresh["replayed_blocks"] == 2
    k2 = fresh["kernel_id"]
    assert w.kernel_exec(k2, "print(x)", timeout=60)["out"].strip() == "42"
    w.kernel_stop(k2)


def test_capture_none_keeps_nothing(w):
    k = w.kernel_start("local", "python", capture="none")["kernel_id"]
    w.kernel_exec(k, "print('ephemeral')", timeout=60)
    w.kernel_stop(k)
    assert w.store.kernel_blocks(k) == []       # opted out, honestly

    bad = w.kernel_start("local", "python", capture="everything")
    assert bad["error"] == "task.invalid"


def test_promote_after_death_from_remains(w):
    """The late-save contract: a kernel that DIED (killed driver, not a
    clean stop) still promotes from its file-protocol remains."""
    k = w.kernel_start("local", "python")["kernel_id"]
    r = w.kernel_exec(
        k, "import os\n"
           "open(os.environ['WEFT_BLOCK_DIR'] + '/v.txt', 'w')"
           ".write('save me later')", timeout=60)
    assert r["rc"] == 0
    # kill the driver process outright — no clean stop
    jobdir = w.store.get_kernel(k)["jobdir"]
    pid = w.adapters["local"].read_file(f"{jobdir}/pid.real").decode().strip()
    subprocess.run(["kill", "-9", pid], check=True)
    for _ in range(80):
        if w.kernel_status(k)["state"] == "died":
            break
        time.sleep(0.25)
    assert w.kernel_status(k)["state"] == "died"

    m = w.kernel_promote(k, blocks=[r["block"]])
    assert m["reproducibility"] == "state-dependent"
    art = next(o for o in m["outputs"] if o["path"].endswith("v.txt"))
    assert art["preview"]["lines"] == ["save me later"]
