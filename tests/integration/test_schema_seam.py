"""Step 1: versioned schemas, reproducibility ladder, push events."""

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def test_manifest_and_provenance_carry_schema(w):
    r = w.task_submit({"command": "echo s > results/o.txt",
                       "outputs": ["results/"], "site": "local"})
    job = w.runner.wait(r["job_id"], 120)
    m = job["manifest"]
    assert m["schema"] == "manifest:v1"
    assert m["reproducibility"] == "task"
    p = w.provenance(r["job_id"])
    assert p["schema"] == "provenance:v1"
    assert p["reproducibility"] == "task"


@pytest.mark.solver
def test_weak_ladder_position(w):
    env = w.env_ensure({"name": "wk", "deps": {"conda": ["xz >=5"]},
                        "post_install": ["true"]})
    r = w.task_submit({"command": "true", "env": env["env_id"],
                       "site": "local"})
    job = w.runner.wait(r["job_id"], 600)
    assert job["manifest"]["reproducibility"] == "weak"


def test_events_subscribe_push(w):
    got = []
    w.events_subscribe(got.append)
    r = w.task_submit({"command": "true", "site": "local"})
    w.runner.wait(r["job_id"], 60)
    kinds = [e["kind"] for e in got if e.get("job_id") == r["job_id"]]
    assert "job.done" in kinds and "job.state" in kinds
    # push and poll agree (same objects, same cursor space)
    polled = [e for e in w.events_poll(0, 300)["events"]
              if e.get("job_id") == r["job_id"]]
    pushed_seqs = {e["seq"] for e in got if e.get("job_id") == r["job_id"]}
    assert {e["seq"] for e in polled} <= pushed_seqs


@pytest.mark.solver
def test_cran_records_carry_dep_graph(w):
    d = w.env_ensure({"name": "g", "deps": {"conda": ["r-base =4.4"],
                                            "cran": ["jsonlite"]},
                      "system_requirements": {"cran_snapshot": "2026-07-01"}},
                     dry_run=True)
    assert "layers" in d, d
    # graph persisted for offline topological installs (B2)
    full = w.env_ensure({"name": "g", "deps": {"conda": ["r-base =4.4"],
                                               "cran": ["jsonlite"]},
                         "system_requirements": {"cran_snapshot": "2026-07-01"}})
    rec = w.env_why(full["env_id"], "jsonlite")["record"]
    assert "deps" in rec and isinstance(rec["deps"], list)
