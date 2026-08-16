"""The scaffold contract, held by REALITY instead of a comment.

_SCAFFOLD_EXACT is the contract that lets a host split "what the run
produced" from weft's plumbing. Its comment said "kept next to the
writers" — but the writers live in the shim script and the runner
template, and the list drifted twice (`pid.epoch` from the
pid-recycling identity check, `node` from the placement round): both
rendered as retainable "files" with byte counts on aba2's Run cards.
This test enumerates REAL jobdirs; the next runner dropping breaks it
here, not in a consumer's UI."""

import pytest

from weft.api import Weft
from weft.retain import is_scaffold


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def _unflagged(root, user_files):
    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file()
        and str(p.relative_to(root)) not in user_files
        and not is_scaffold(str(p.relative_to(root))))


def test_task_jobdir_is_fully_classified(w, tmp_path):
    jid = w.task_submit({"command": "echo real > out.txt",
                         "outputs": ["out.txt"], "site": "local"})["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    jobdir = tmp_path / "site" / "jobs" / jid
    stray = _unflagged(jobdir, {"out.txt"})
    assert not stray, (
        f"weft-written files the scaffold contract misses: {stray} — "
        "add them to _SCAFFOLD_EXACT (they render as retainable user "
        "files in receipt-driven UIs)")
    # and the receipt agrees: only the user file is unflagged there too
    inv = w.run_inventory(jid)
    for e in inv["entries"]:
        if e["path"] != "out.txt":
            assert e.get("scaffold"), e


def test_kernel_jobdir_is_fully_classified(w, tmp_path):
    k = w.kernel_start("local", "python")["kernel_id"]
    r = w.kernel_exec(
        k, "import os\n"
           "open(os.environ['WEFT_BLOCK_DIR'] + '/prod.txt', 'w')"
           ".write('user artifact')")
    assert r["rc"] == 0
    w.kernel_stop(k)
    kd = tmp_path / "site" / w.store.get_kernel(k)["jobdir"]
    stray = [f for f in _unflagged(kd, set())
             if ".artifacts/" not in f]      # block artifacts = user files
    assert not stray, (
        f"weft-written kernel files the scaffold contract misses: {stray}")
