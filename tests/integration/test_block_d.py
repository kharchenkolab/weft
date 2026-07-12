"""Block D: log following and rich structure previews."""

import time

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def test_log_follow_incremental(w):
    r = w.task_submit({
        "command": "for i in 1 2 3 4 5; do echo tick-$i; sleep 1; done",
        "site": "local"})
    job_id = r["job_id"]
    seen, cursor = "", 0
    deadline = time.time() + 60
    while time.time() < deadline:
        out = w.task_logs(job_id, follow_cursor=cursor)
        assert cursor + len(out["log"].encode()) == out["cursor"]  # no gaps
        seen += out["log"]
        cursor = out["cursor"]
        if out["state"] in ("DONE", "FAILED"):
            break
        time.sleep(0.5)
    lines = [l for l in seen.splitlines() if l.startswith("tick-")]
    assert lines == [f"tick-{i}" for i in range(1, 6)]  # ordered, no dupes
    # plain tail mode still works and reports state
    tail = w.task_logs(job_id, tail=2)
    assert "tick-5" in tail["log"] and tail["state"] == "DONE"


@pytest.mark.solver
@pytest.mark.slow
def test_hdf5_rich_preview(w):
    env = w.env_ensure({"name": "h5env",
                        "deps": {"conda": ["python =3.12", "h5py", "numpy"]}})
    assert "env_id" in env, env
    r = w.task_submit({
        "command": "python -c \"import h5py, numpy as np; "
                   "f = h5py.File('results/scan.h5', 'w'); "
                   "f.create_dataset('fit/chi2', data=np.zeros((100, 3))); "
                   "f.create_dataset('meta/grid', data=np.arange(7)); "
                   "f.close()\"",
        "env": env["env_id"], "outputs": ["results/"], "site": "local"})
    job = w.runner.wait(r["job_id"], 1800)
    assert job["state"] == "DONE", job["error"]
    h5 = next(o for o in job["manifest"]["outputs"]
              if o["path"] == "results/scan.h5")
    assert h5["preview"]["kind"] == "hdf5-tree", h5["preview"]
    assert "fit/chi2 (100, 3)" in h5["preview"]["detail"]
    assert "meta/grid (7,)" in h5["preview"]["detail"]


@pytest.mark.solver
@pytest.mark.slow
def test_rds_rich_preview(w):
    env = w.env_ensure({"name": "rdsenv", "deps": {"conda": ["r-base =4.4"]}})
    assert "env_id" in env, env
    r = w.task_submit({
        "command": "Rscript -e 'saveRDS(list(fit=1:10, tag=\"run\"), "
                   "\"results/fit.rds\")'",
        "env": env["env_id"], "outputs": ["results/"], "site": "local"})
    job = w.runner.wait(r["job_id"], 1800)
    assert job["state"] == "DONE", job["error"]
    rds = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/fit.rds")
    assert rds["preview"]["kind"] == "rds-str", rds["preview"]
    assert "List of 2" in rds["preview"]["detail"]
