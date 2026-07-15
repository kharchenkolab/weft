"""wait() on a recovered job (found live: a clip job sat COMPLETED in
slurm while weft's row stayed QUEUED — the submitting process had died
and no poller in the new process was watching; wait() spun forever)."""

import pytest

from weft.api import Weft


def test_wait_from_a_fresh_process_collects(tmp_path, pixi_bin):
    w1 = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w1.register_site("local", "local", {"root": str(tmp_path / "site"),
                                        "pixi_source": pixi_bin})
    w1.runner.poll_interval = 0.2
    r = w1.task_submit({"command": "sleep 1; echo fin > results/x.txt",
                        "outputs": ["results/"], "site": "local"})
    # the submitting "process" goes away without ever collecting
    del w1

    w2 = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w2.runner.poll_interval = 0.2
    job = w2.runner.wait(r["job_id"], 120)      # reconciles, then collects
    assert job["state"] == "DONE", job.get("error")
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/x.txt")
    assert out["preview"]["lines"] == ["fin"]
