"""Block A ledger items: oom allocation size, memoized markers, idle GC."""

import time

import pytest

from weft.api import Weft
from weft.classify import classify_log


def test_classifier_extracts_failed_allocation():
    tail = ("Traceback (most recent call last):\n  ...\n"
            "numpy._core._exceptions._ArrayMemoryError: "
            "Unable to allocate 1.40 GiB for an array with shape...")
    sig = classify_log(tail)
    assert sig["failed_allocation"] == "1.40 GiB"
    assert classify_log("MemoryError").get("failed_allocation") is None


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def test_memoized_elements_are_marked(w):
    t = {"command": "echo m-$WEFT_ARRAY_INDEX > results/o.txt",
         "outputs": ["results/"], "site": "local", "array": 2}
    r1 = w.task_submit(t)
    for sub in r1["jobs"]:
        w.runner.wait(sub["job_id"], 300)
    r2 = w.task_submit(t)
    st = w.array_status(r2["group"])
    assert all(e.get("memoized") for e in st["elements"]), st["elements"]
    st1 = w.array_status(r1["group"])
    assert not any(e.get("memoized") for e in st1["elements"])


def test_kernel_idle_autostop_policy(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {
        "root": str(tmp_path / "site"), "pixi_source": pixi_bin,
        "policy": {"kernel_idle_stop_s": 2}})
    w.runner.poll_interval = 0.3
    k = w.kernel_start("local", "python")["kernel_id"]
    assert w.kernel_exec(k, "z = 1")["rc"] == 0
    deadline = time.time() + 30
    while time.time() < deadline:
        if w.kernel_status(k)["state"] == "stopped":
            break
        time.sleep(0.5)
    assert w.kernel_status(k)["state"] == "stopped"
    kinds = [e["kind"] for e in w.events_poll(0, 300)["events"]]
    assert "kernel.idle_stopped" in kinds
