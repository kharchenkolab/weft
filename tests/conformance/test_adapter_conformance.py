"""Adapter conformance (doc 06 §2): every adapter must produce identical
manifests, output identities, lifecycle event sequences, and error causes
for a canonical task set. The local adapter is the oracle; new adapters
must pass this suite before merge.

Because outputs are content-addressed, "identical results" is checkable
exactly: the same task must yield the same DataRefs on every adapter.
"""

import pytest

from weft.api import Weft


@pytest.fixture(params=["local", "ssh"])
def site(request, tmp_path, pixi_bin, sshd_site=None):
    if request.param == "ssh":
        sshd = request.getfixturevalue("sshd_site")
        w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
        w.register_site("target", "ssh", {
            "host": sshd["host"], "port": sshd["port"], "user": sshd["user"],
            "ssh_opts": sshd["ssh_opts"],
            # per-test isolation inside the shared container
            "root": f"/home/physicist/.weft-{tmp_path.name}",
            "pixi_source": pixi_bin,
        })
    else:
        w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
        w.register_site("target", "local", {"root": str(tmp_path / "site"),
                                            "pixi_source": pixi_bin})
    return w


CANONICAL_CMD = (
    "printf 'x,y\\n1,2\\n3,4\\n' > results/table.csv; "
    "printf '{\"chi2\": 1.25}' > results/fit.json; "
    "cat data/in.txt > results/echo.txt"
)


def _run_canonical(w: Weft):
    (w.workspace / "in.txt").write_text("payload-42\n")
    ref = w.data_register("in.txt")["ref"]
    r = w.task_submit({
        "command": CANONICAL_CMD,
        "inputs": [{"ref": ref, "mount_as": "data/in.txt"}],
        "outputs": ["results/"],
        "site": "target",
        "env_vars": {"ANALYSIS_TAG": "conformance"},
    })
    assert "job_id" in r, r
    job = w.runner.wait(r["job_id"], 180)
    return r, job


# collected once by the local (oracle) run, compared by every other adapter
_ORACLE: dict = {}


@pytest.mark.docker
def test_canonical_task_identical_everywhere(site):
    r, job = _run_canonical(site)
    assert job["state"] == "DONE", job["error"]
    m = job["manifest"]
    outputs = {o["path"]: o["ref"] for o in m["outputs"]}
    events = [e["state"] for e in site.events_poll(0, 500)["events"]
              if e["kind"] == "job.state" and e.get("job_id") == r["job_id"]]
    snapshot = {
        "outputs": outputs,
        "exit_code": m["exit_code"],
        "preview_kinds": {o["path"]: o["preview"]["kind"] for o in m["outputs"]},
        "events": events,
    }
    if "oracle" not in _ORACLE:
        _ORACLE["oracle"] = snapshot
    else:
        oracle = _ORACLE["oracle"]
        # content-addressed identity: byte-identical results across adapters
        assert snapshot["outputs"] == oracle["outputs"]
        assert snapshot["exit_code"] == oracle["exit_code"]
        assert snapshot["preview_kinds"] == oracle["preview_kinds"]
        assert snapshot["events"] == oracle["events"]


_FAIL_ORACLE: dict = {}


@pytest.mark.docker
def test_failure_cause_identical_everywhere(site):
    r = site.task_submit({
        "command": "python3 -c 'raise RuntimeError(\"detector misaligned\")'",
        "site": "target",
    })
    job = site.runner.wait(r["job_id"], 120)
    assert job["state"] == "FAILED"
    err = job["error"]
    snapshot = {
        "code": err["error"],
        "stage": err["stage"],
        "signature": err["hints"]["log_signature"]["signature"],
    }
    if "oracle" not in _FAIL_ORACLE:
        _FAIL_ORACLE["oracle"] = snapshot
    else:
        assert snapshot == _FAIL_ORACLE["oracle"]


@pytest.mark.docker
def test_sandbox_layout_contract(site):
    """Guaranteed env vars and directory layout, identical everywhere."""
    r = site.task_submit({
        "command": "echo \"$WEFT_JOB_ID|$WEFT_CPUS|$WEFT_MEM_GB\" > results/vars.txt; "
                   "test -d tmp && echo tmpdir >> results/vars.txt; "
                   "pwd | grep -q jobs/ && echo cwd-in-jobdir >> results/vars.txt",
        "outputs": ["results/"],
        "resources": {"cpus": 2, "mem_gb": 1},
        "site": "target",
    })
    job = site.runner.wait(r["job_id"], 120)
    assert job["state"] == "DONE", job["error"]
    vars_out = next(o for o in job["manifest"]["outputs"]
                    if o["path"] == "results/vars.txt")
    lines = vars_out["preview"]["lines"]
    assert lines[0].endswith("|2|1") and lines[0].startswith("jb_")
    assert "tmpdir" in lines and "cwd-in-jobdir" in lines
