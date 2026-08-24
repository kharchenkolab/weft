"""Directory rels for the (run, relpath) key (escalated ask, 3 live
hits): stat_candidate/stat_batch tested [ -f ] only, so a .zarr-class
store in a kernel sandbox read as ABSENT and data_register(run=, rel=)
refused with data.missing — the durable handle was unusable for
exactly the artifact class it matters most for. Directories are now
first-class across EVERY (run, rel) consumer (the conformance table):
stats answer kind:"dir" (no bytes — a tree's size is a walk); the
register door mints the IDENTICAL tree ref the absolute-path door
does; reads refuse TYPED with the levers named; keep-side dirs
resolve. The carried rc-trust wart is paid: a stat probe with no
verdict raises retryable, never reads as absent."""

import os
import time
from pathlib import Path

import pytest

from weft.api import Weft
from weft.errors import WeftError
from weft.fileio import stat_candidate


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def _kernel_with_store(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    r = w.kernel_exec(
        k, "import os\n"
           "os.makedirs('viewer.lstar.zarr/g1', exist_ok=True)\n"
           "open('viewer.lstar.zarr/.zattrs', 'w').write('{}')\n"
           "open('viewer.lstar.zarr/g1/0.0', 'w').write('chunk-bytes')\n"
           "open('summary.txt', 'w').write('done')", timeout=60)
    assert r["rc"] == 0
    return k


def test_dir_rel_registers_as_tree_identical_to_absolute_door(w, tmp_path):
    """The reported scenario, red-first on HEAD: register the store by
    (run, rel) — and pin IDENTITY EQUALITY with the absolute-path door
    (th_3a7d3c5e did both; the refs must be the same tree)."""
    k = _kernel_with_store(w)
    out = w.data_register(run=k, rel="viewer.lstar.zarr")
    assert "error" not in out, out
    assert out["kind"] == "tree"
    jd = Path(w.adapters["local"].path(w.store.get_kernel(k)["jobdir"]))
    absolute = w.data_register(str(jd / "viewer.lstar.zarr"))
    assert absolute["ref"] == out["ref"]        # one identity, two doors
    members = w.data_members(out["ref"])
    paths = {m["path"] for m in members["members"]}
    assert paths == {".zattrs", "g1/0.0"}
    w.kernel_stop(k)


def test_run_file_stat_answers_kind_dir(w):
    k = _kernel_with_store(w)
    st = w.run_file_stat(k, rel="viewer.lstar.zarr")
    assert st["exists"] and st["kind"] == "dir"
    assert "bytes" not in st                    # a walk, not a stat
    assert isinstance(st["mtime"], int)
    # files unchanged, kind added
    stf = w.run_file_stat(k, rel="summary.txt")
    assert stf["kind"] == "file" and stf["bytes"] == 4
    w.kernel_stop(k)


def test_batched_stat_mixes_kinds(w):
    k = _kernel_with_store(w)
    out = w.run_file_stat(k, rels=["viewer.lstar.zarr", "summary.txt",
                                   "ghost.txt"])
    f = out["files"]
    assert f["viewer.lstar.zarr"]["kind"] == "dir"
    assert "bytes" not in f["viewer.lstar.zarr"]
    assert f["summary.txt"]["kind"] == "file"
    assert f["ghost.txt"]["exists"] is False
    w.kernel_stop(k)


def test_reads_refuse_dirs_typed(w):
    k = _kernel_with_store(w)
    r = w.run_file_read(k, "viewer.lstar.zarr")
    assert r["error"] == "task.invalid" and "directory" in r["detail"]
    assert "data_register" in str(r["hints"]["levers"]["register"])
    rr = w.run_file_read_range(k, rel="viewer.lstar.zarr", offset=0,
                               length=16)
    assert rr["error"] == "task.invalid" and "directory" in rr["detail"]
    # a MEMBER of the dir reads fine (the lever works)
    m = w.run_file_read(k, "viewer.lstar.zarr/g1/0.0")
    assert "error" not in m and m["bytes_total"] == 11
    w.kernel_stop(k)


def test_keep_side_dir_resolves_after_sweep(w):
    """The durable-handle promise: retain the store, destroy the
    sandbox — (run, rel) still mints the tree from the KEEP."""
    k = _kernel_with_store(w)
    ref_live = w.data_register(run=k, rel="viewer.lstar.zarr")["ref"]
    w.kernel_stop(k)
    kept = w.run_retain(k, include=["viewer.lstar.zarr"],
                        dest="@workspace", background=False)
    assert kept["state"] == "done"
    w.run_discard(k)                            # sandbox gone
    st = w.run_file_stat(k, rel="viewer.lstar.zarr")
    assert st["exists"] and st["kind"] == "dir" and st["at"] == "retained"
    again = w.data_register(run=k, rel="viewer.lstar.zarr")
    assert again["ref"] == ref_live             # same identity from keep


def test_probe_failure_raises_never_absent():
    """The paid wart: a remote stat probe with no verdict is probe
    trouble (retryable), never exists:false."""
    class R:
        rc, out, err = 1, "", "ssh: connection reset"

    class FakeAdapter:
        def run_cmd(self, *a, **kw):
            return R()

    with pytest.raises(WeftError) as ei:
        stat_candidate({"adapter": FakeAdapter(), "path": "/x/y",
                        "root": "/x"})
    assert ei.value.code == "internal.error" and ei.value.retryable


def test_remote_marker_grammar_parses_all_arms():
    """Conformance for the DIR/file/ABSENT marker vocabulary on the
    single-candidate probe."""
    class FakeAdapter:
        def __init__(self, out):
            self._out = out

        def run_cmd(self, *a, **kw):
            class R:
                rc, err = 0, ""
            R.out = self._out
            return R()

    d = stat_candidate({"adapter": FakeAdapter("DIR 1700000000\n"),
                        "path": "/x/d", "root": "/x"})
    assert d == {"kind": "dir", "mtime": 1700000000}
    f = stat_candidate({"adapter": FakeAdapter("42 1700000001\n"),
                        "path": "/x/f", "root": "/x"})
    assert f == {"kind": "file", "bytes": 42, "mtime": 1700000001}
    a = stat_candidate({"adapter": FakeAdapter("ABSENT\n"),
                        "path": "/x/a", "root": "/x"})
    assert a is None
