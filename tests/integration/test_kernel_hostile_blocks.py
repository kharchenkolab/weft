"""Hostile-ambient-state battery (mendel incident: a block's setwd
killed two kernels in a row). The kernel driver shares its PROCESS
with arbitrary user code — every ambient global it relies on after
block N is mutable by block N. Cooperative-only block corpora ratify
the happy path (field note #5's lesson, applied to the least-validated
user input in the system: kernel code)."""

import base64

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def _exec(w, k, code, want_rc=0):
    r = w.kernel_exec(k, code, timeout=60)
    assert r["rc"] == want_rc, r
    return r


def test_chdir_block_cannot_kill_the_kernel(w, tmp_path):
    """THE incident, python shape: an ordinary mkdir+chdir block. The
    kernel must survive, the NEXT block must run, cwd must PERSIST
    (session state — the user asked for it), and artifacts must still
    land in the sandbox's blocks/ dir, not the new cwd."""
    work = tmp_path / "elsewhere" / "deep"
    k = w.kernel_start("local", "python")["kernel_id"]
    try:
        _exec(w, k, f"import os\n"
                    f"os.makedirs({str(work)!r}, exist_ok=True)\n"
                    f"os.chdir({str(work)!r})")
        # the kernel is ALIVE and the next block executes
        r = _exec(w, k, "import os; print(os.getcwd())")
        assert r["out"].strip() == str(work)      # cwd persisted
        # artifacts still land in the SANDBOX, absolute WEFT_BLOCK_DIR
        _exec(w, k, "import os\n"
                    "p = os.environ['WEFT_BLOCK_DIR']\n"
                    "assert os.path.isabs(p), p\n"
                    "open(os.path.join(p, 'saved.txt'), 'w')"
                    ".write('kept')")
        st = w.kernel_status(k)
        assert st["state"] == "running", st
        jobdir = tmp_path / "site" / "kernels" / k
        arts = list((jobdir / "blocks").glob("*.artifacts/saved.txt"))
        assert arts and arts[0].read_text() == "kept"
        assert not (work / "blocks").exists()     # nothing scattered
    finally:
        w.kernel_stop(k)


def test_chdir_to_deleted_dir_still_survives(w, tmp_path):
    """Worse than the incident: the block's cwd no longer EXISTS. The
    driver's absolute protocol paths must not care."""
    k = w.kernel_start("local", "python")["kernel_id"]
    try:
        _exec(w, k, f"import os\n"
                    f"d = {str(tmp_path / 'doomed')!r}\n"
                    f"os.makedirs(d, exist_ok=True)\n"
                    f"os.chdir(d)\n"
                    f"os.rmdir(d)")
        r = _exec(w, k, "print('still alive')")
        assert "still alive" in r["out"]
    finally:
        w.kernel_stop(k)


def test_stdout_replacement_does_not_break_capture(w):
    """A block that permanently replaces sys.stdout: the NEXT block's
    output must still be captured (the driver re-redirects per
    block)."""
    k = w.kernel_start("local", "python")["kernel_id"]
    try:
        _exec(w, k, "import sys, io\nsys.stdout = io.StringIO()")
        r = _exec(w, k, "print('captured')")
        assert "captured" in r["out"]
    finally:
        w.kernel_stop(k)


def test_block_dir_clobber_is_reset_next_block(w):
    """WEFT_BLOCK_DIR is re-exported per block: a block that clobbers
    it only hurts itself."""
    k = w.kernel_start("local", "python")["kernel_id"]
    try:
        _exec(w, k, "import os\nos.environ['WEFT_BLOCK_DIR'] = '/nope'")
        r = _exec(w, k, "import os\n"
                        "print(os.environ['WEFT_BLOCK_DIR'])")
        assert "/nope" not in r["out"]
        assert "blocks/" in r["out"]
    finally:
        w.kernel_stop(k)


def test_prelude_chdir_cannot_orphan_the_exit_record(w, tmp_path,
                                                     pixi_bin):
    """The sweep's sibling: site_prelude is SOURCED into runner.sh —
    a prelude that chdirs (module inits do) must not send exit_code/
    wall_s/log to the wrong directory (the job would never settle)."""
    w2 = Weft(tmp_path / "ws2", pixi_bin=pixi_bin)
    w2.register_site("local", "local", {
        "root": str(tmp_path / "site2"), "pixi_source": pixi_bin,
        "site_prelude": "cd /tmp"})
    w2.runner.poll_interval = 0.2
    r = w2.task_submit({"command": "echo anchored > out.txt",
                        "outputs": ["out.txt"], "site": "local"})
    job = w2.runner.wait(r["job_id"], 120)
    assert job["state"] == "DONE", job.get("error")
    jd = tmp_path / "site2" / "jobs" / r["job_id"]
    assert (jd / "exit_code").read_text().strip() == "0"
    assert (jd / "wall_s").exists() and (jd / "log").exists()
    assert not (tmp_path / "site2" / "exit_code").exists()
