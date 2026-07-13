"""Job labels: a human handle on tasks — display-only by design.

A label says what to CALL a task, not what it computes: it is excluded
from task_hash (like `after`), so relabeling never forks memoization
and identically-labeled tasks still memoize against each other.
"""

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def test_label_surfaces_everywhere(w):
    r = w.task_submit({"command": "true", "site": "local",
                       "label": "calibrate run 3"})
    assert w.runner.wait(r["job_id"], 60)["state"] == "DONE"

    st = w.task_status(r["job_id"])[0]
    assert st["label"] == "calibrate run 3"
    row = next(j for j in w.jobs_where(limit=50)["jobs"]
               if j["job_id"] == r["job_id"])
    assert row["label"] == "calibrate run 3"
    assert row["task"]["label"] == "calibrate run 3"
    ev = next(e for e in w.events_poll(0, 500, compact=False)["events"]
              if e["kind"] == "job.state" and e.get("job_id") == r["job_id"]
              and e.get("state") == "PENDING")
    assert ev["label"] == "calibrate run 3"

    # unlabeled tasks read as None, not empty string
    r2 = w.task_submit({"command": "echo x", "site": "local"})
    assert w.runner.wait(r2["job_id"], 60)["state"] == "DONE"
    assert w.task_status(r2["job_id"])[0]["label"] is None


def test_label_is_memoization_neutral(w):
    r1 = w.task_submit({"command": "echo stable > results/o.txt",
                        "outputs": ["results/"], "site": "local",
                        "label": "first name"})
    assert w.runner.wait(r1["job_id"], 60)["state"] == "DONE"
    r2 = w.task_submit({"command": "echo stable > results/o.txt",
                        "outputs": ["results/"], "site": "local",
                        "label": "totally different name"})
    assert r2.get("memoized") is True, r2
    assert r2["job_id"] == r1["job_id"]  # the prior job, prior label


def test_array_group_carries_the_label(w):
    r = w.task_submit({"command": "true", "array": 3, "site": "local",
                       "label": "sweep alpha"})
    import time
    for _ in range(100):
        if w.store.group_counts(r["group"])["done"] == 3:
            break
        time.sleep(0.2)
    st = w.array_status(r["group"])
    assert st["label"] == "sweep alpha"
    el = w.store.jobs_in_group(r["group"])[0]
    assert w.task_status(el["job_id"])[0]["label"] == "sweep alpha"


def test_oversized_label_refused(w):
    out = w.task_submit({"command": "true", "site": "local",
                         "label": "x" * 201})
    assert out["error"] == "task.invalid"
    assert "label" in out["detail"]
