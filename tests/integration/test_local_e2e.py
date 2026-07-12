"""End-to-end local execution through the agent API (the conformance oracle).

Fast tests use env=None (bare site environment) so no solver is needed;
the full pixi path is exercised once under the `solver` marker.
"""

import json
import time

import pytest

from weft.api import Weft


@pytest.fixture
def weft(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def _wait(w: Weft, job_id: str, timeout=60):
    return w.runner.wait(job_id, timeout)


def test_submit_produces_manifest_and_provenance(weft, tmp_path):
    data = tmp_path / "ws" / "samples.csv"
    data.write_text("t,v\n" + "\n".join(f"{i},{i * i}" for i in range(100)) + "\n")
    ref = weft.data_register("samples.csv")["ref"]

    r = weft.task_submit({
        "command": "wc -l < data/samples.csv > results/count.txt; "
                   "echo '{\"peak\": 42.0}' > results/fit.json",
        "inputs": [{"ref": ref, "mount_as": "data/samples.csv"}],
        "outputs": ["results/"],
        "site": "local",
    })
    assert "job_id" in r, r
    assert r["plan"]["staging"]["bytes_to_move"] > 0  # first time: bytes move
    job = _wait(weft, r["job_id"])
    assert job["state"] == "DONE", job["error"]
    m = job["manifest"]
    assert m["exit_code"] == 0
    paths = {o["path"] for o in m["outputs"]}
    assert "results/count.txt" in paths and "results/fit.json" in paths
    fit = next(o for o in m["outputs"] if o["path"] == "results/fit.json")
    assert fit["preview"]["kind"] == "inline-json"
    assert fit["preview"]["value"]["peak"] == 42.0
    count = next(o for o in m["outputs"] if o["path"] == "results/count.txt")
    assert count["preview"]["lines"] == ["101"]  # 100 rows + header


def test_memoization_and_force(weft, tmp_path):
    r1 = weft.task_submit({"command": "echo done > results/out.txt",
                           "outputs": ["results/"], "site": "local"})
    _wait(weft, r1["job_id"])
    r2 = weft.task_submit({"command": "echo done > results/out.txt",
                           "outputs": ["results/"], "site": "local"})
    assert r2.get("memoized") is True and r2["job_id"] == r1["job_id"]
    r3 = weft.task_submit({"command": "echo done > results/out.txt",
                           "outputs": ["results/"], "site": "local"}, force=True)
    assert r3.get("memoized") is None and r3["job_id"] != r1["job_id"]
    _wait(weft, r3["job_id"])


def test_failure_carries_structured_cause(weft):
    r = weft.task_submit({
        "command": "python3 -c 'import missing_dependency_xyz'",
        "site": "local",
    })
    job = _wait(weft, r["job_id"])
    assert job["state"] == "FAILED"
    err = job["error"]
    assert err["error"] == "job.nonzero_exit"
    assert err["hints"]["log_signature"]["signature"] in (
        "python-traceback", "python-module-missing",
    )
    assert "log_tail" in err["hints"]


def test_output_chaining_no_retransfer(weft):
    """Task N+1 consuming task N's output must find it already present."""
    r1 = weft.task_submit({
        "command": "seq 1 1000 > results/series.txt",
        "outputs": ["results/"], "site": "local",
    })
    job1 = _wait(weft, r1["job_id"])
    series_ref = next(o["ref"] for o in job1["manifest"]["outputs"]
                      if o["path"] == "results/series.txt")
    # the output was ingested site-side: a dependent task's plan moves 0 bytes
    r2 = weft.task_submit({
        "command": "awk '{s+=$1} END {print s}' data/series.txt > results/sum.txt",
        "inputs": [{"ref": series_ref, "mount_as": "data/series.txt"}],
        "outputs": ["results/"], "site": "local",
    })
    assert r2["plan"]["staging"]["bytes_to_move"] == 0
    assert series_ref in r2["plan"]["staging"]["already_present"]
    job2 = _wait(weft, r2["job_id"])
    sum_out = next(o for o in job2["manifest"]["outputs"]
                   if o["path"] == "results/sum.txt")
    assert sum_out["preview"]["lines"] == ["500500"]


def test_walltime_enforced_uniformly(weft):
    r = weft.task_submit({
        "command": "sleep 60",
        "resources": {"walltime": "00:00:01"},
        "site": "local",
    })
    job = _wait(weft, r["job_id"], timeout=45)
    assert job["state"] == "FAILED"
    assert job["error"]["error"] == "job.walltime_exceeded"
    assert "suggestion" in job["error"]["hints"]


def test_oom_classified_with_sizing_hints(weft):
    """A memory kill must come back as job.oom with the observed-vs-asked
    numbers the agent needs to right-size the resubmission (doc 05 §3)."""
    r = weft.task_submit({
        "command": "ulimit -v 262144; "
                   "python3 -c 'x = bytearray(1024*1024*1024)'",
        "resources": {"mem_gb": 1},
        "site": "local",
    })
    job = _wait(weft, r["job_id"])
    assert job["state"] == "FAILED"
    err = job["error"]
    assert err["error"] == "job.oom"
    assert "resubmit with a larger mem_gb" in err["hints"]["suggestion"]
    assert err["hints"]["requested_gb"] == 1


def test_cancel(weft):
    r = weft.task_submit({"command": "sleep 300", "site": "local"})
    time.sleep(1.5)
    weft.task_cancel(r["job_id"])
    job = _wait(weft, r["job_id"], timeout=20)
    assert job["state"] == "CANCELLED"


def test_array_fanout(weft):
    r = weft.task_submit({
        "command": "echo \"element $WEFT_ARRAY_INDEX\" > results/elem.txt",
        "outputs": ["results/"], "site": "local", "array": 4,
    })
    assert r["elements"] == 4 and len(r["jobs"]) == 4
    seen = set()
    for sub in r["jobs"]:
        job = _wait(weft, sub["job_id"])
        assert job["state"] == "DONE"
        elem = next(o for o in job["manifest"]["outputs"]
                    if o["path"] == "results/elem.txt")
        seen.add(elem["preview"]["lines"][0])
    assert seen == {f"element {i}" for i in range(4)}


def test_events_feed_and_audit(weft):
    r = weft.task_submit({"command": "true", "site": "local"})
    _wait(weft, r["job_id"])
    feed = weft.events_poll(0, limit=200)
    kinds = [e["kind"] for e in feed["events"] if e.get("job_id") == r["job_id"]]
    assert "job.state" in kinds and "job.done" in kinds
    states = [e["state"] for e in feed["events"]
              if e.get("job_id") == r["job_id"] and e["kind"] == "job.state"]
    assert states[0] == "PENDING" and "RUNNING" in states
    # incremental cursor: nothing new after draining
    assert weft.events_poll(feed["cursor"])["events"] == []


def test_capability_violation_hints(weft):
    r = weft.task_submit({
        "command": "true", "site": "local",
        "resources": {"cpus": 100000},
    })
    assert r["error"] == "site.capability_violation"
    assert r["hints"]["cpus"]["max"] >= 1


def test_guarded_shell(weft):
    out = weft.site_exec("local", "ls -la", why="inspect weft root layout")
    assert out["rc"] == 0 and "envs" in out["stdout"]
    bad = None
    try:
        weft.site_exec("local", "rm -rf /etc", why="oops")
    except Exception as e:
        bad = e
    assert bad is not None
    # denial is audited
    tail = weft.store.audit_tail()
    assert any(a["action"] == "site.exec.DENIED" for a in tail)
    # why is mandatory
    try:
        weft.site_exec("local", "ls", why="")
        raised = False
    except Exception:
        raised = True
    assert raised


@pytest.mark.solver
def test_full_pixi_env_path(weft, tmp_path):
    ensured = weft.env_ensure({
        "name": "py-mini",
        "deps": {"conda": ["python =3.12"]},
        "env_vars": {"WEFT_TEST_VAR": "{{cpus}}"},
    })
    assert "env_id" in ensured, ensured
    env_id = ensured["env_id"]

    r = weft.task_submit({
        "command": "python -c \"import sys, os, json; "
                   "json.dump({'py': sys.version.split()[0], "
                   "'threads': os.environ['WEFT_TEST_VAR']}, "
                   "open('results/info.json','w'))\"",
        "env": env_id,
        "outputs": ["results/"],
        "resources": {"cpus": 2},
        "site": "local",
    })
    assert "job_id" in r, r
    assert r["plan"]["env"]["action"] == "build"
    job = _wait(weft, r["job_id"], timeout=600)
    assert job["state"] == "DONE", json.dumps(job["error"], indent=2)
    info = next(o for o in job["manifest"]["outputs"]
                if o["path"] == "results/info.json")
    assert info["preview"]["value"]["py"].startswith("3.12")
    assert info["preview"]["value"]["threads"] == "2"

    # cache hit: same env resubmission plans "cached" and starts fast
    ensured2 = weft.env_ensure({
        "name": "py-mini",
        "deps": {"conda": ["python =3.12"]},
        "env_vars": {"WEFT_TEST_VAR": "{{cpus}}"},
    })
    assert ensured2["status"] == "cached" and ensured2["env_id"] == env_id
    r2 = weft.task_submit({
        "command": "python -c 'print(1)'", "env": env_id, "site": "local",
    })
    assert r2["plan"]["env"]["action"] == "cached"
    job2 = _wait(weft, r2["job_id"], timeout=120)
    assert job2["state"] == "DONE"
