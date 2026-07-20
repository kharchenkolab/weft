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


# ── file-protocol invariant (bug2): atomic publish, per adapter ────────────
# Any file a concurrent reader consumes on first sight must be complete the
# moment it exists (documentation/architecture.md, Kernels;
# misc/polled_files_audit.md). Both fast-lane adapters execute their REAL
# write path here: LocalAdapter its python path, SSHAdapter its actual shell
# string via transport="local" (what ssh would hand the remote shell, sh
# gets locally — no docker needed). The remote-ssh reader side is pinned by
# tests/integration/test_kernel_block_race.py.

import threading

from weft.adapters.local import LocalAdapter
from weft.adapters.ssh import SSHAdapter


@pytest.fixture(params=["local", "ssh-shell"])
def raw_adapter(request, tmp_path):
    root = tmp_path / "araw"
    if request.param == "local":
        return LocalAdapter("l", root), root
    return SSHAdapter("s", "unused-host", str(root), transport="local"), root


def test_write_file_existence_is_completeness(raw_adapter):
    ad, root = raw_adapter
    payload = b"y" * (1 << 20)
    p = root / "blocks" / "0000.code"
    caught = []
    for _ in range(10):
        p.unlink(missing_ok=True)
        stop = threading.Event()

        def watch():
            while True:
                if p.exists():
                    caught.append(p.read_bytes())
                    return
                if stop.is_set():
                    return

        t = threading.Thread(target=watch)
        t.start()
        ad.write_file("blocks/0000.code", payload)
        stop.set()
        t.join(10)
    assert len(caught) == 10 and all(c == payload for c in caught)
    assert list((root / "blocks").glob("*.wtmp.*")) == []


def test_write_file_identical_result_everywhere(tmp_path):
    """Conformance spirit: same bytes, same mode, from both write paths."""
    la, sa = (LocalAdapter("l", tmp_path / "A"),
              SSHAdapter("s", "unused", str(tmp_path / "B"), transport="local"))
    for ad in (la, sa):
        ad.write_file("d/f.bin", b"\x00\x01payload", mode=0o640)
    a, b = (tmp_path / "A/d/f.bin"), (tmp_path / "B/d/f.bin")
    assert a.read_bytes() == b.read_bytes() == b"\x00\x01payload"
    assert (a.stat().st_mode & 0o777) == (b.stat().st_mode & 0o777) == 0o640
