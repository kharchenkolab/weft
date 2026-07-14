"""Declared outputs may be plain files (user-model ask): the single
figure/table is the most common step output — no mkdir boilerplate, no
run-then-fail-at-collection surprise. Directories stay the convention
for multi-artifact steps."""

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def test_shim_hash_tree_accepts_file_root(w, tmp_path):
    adapter = w.adapters["local"]
    f = tmp_path / "site-file.txt"
    f.write_text("one artifact\n")
    r = adapter.shim(["hash-tree", "--root", str(f)])
    assert r.rc == 0
    rows = r.out.splitlines()
    assert len(rows) == 1
    kind, path, is_exec, size, digest = rows[0].split("\t")
    assert (kind, path, is_exec) == ("file", ".", "0")
    assert int(size) == len("one artifact\n") and len(digest) == 64
    # genuinely missing roots still refuse, with an honest message
    r = adapter.shim(["hash-tree", "--root", str(tmp_path / "nope")])
    assert r.rc != 0 and "no such file or directory" in r.err


def test_single_file_output_end_to_end(w):
    r = w.task_submit({"command": "printf 'sum,42\\n' > table.csv",
                       "outputs": ["table.csv"], "site": "local"})
    job = w.runner.wait(r["job_id"], 120)
    assert job["state"] == "DONE", job["error"]
    outs = job["manifest"]["outputs"]
    entry = next(o for o in outs if o["path"] == "table.csv")
    assert entry["bytes"] == 7
    assert entry["preview"]["header"] == "sum,42"   # csv → table preview
    # a file output is a plain file entry: no tree row for it
    assert not any(o["path"].startswith("table.csv/") for o in outs)

    # chains: a downstream task mounts the file ref and stages 0 bytes
    r2 = w.task_submit({"command": "wc -c < in.csv > n.txt",
                        "inputs": [{"ref": entry["ref"],
                                    "mount_as": "in.csv"}],
                        "outputs": ["n.txt"], "site": "local"})
    j2 = w.runner.wait(r2["job_id"], 120)
    assert j2["state"] == "DONE", j2["error"]
    n = next(o for o in j2["manifest"]["outputs"] if o["path"] == "n.txt")
    assert n["preview"]["lines"][0].strip() == "7"


def test_mixed_file_and_dir_outputs(w):
    r = w.task_submit({
        "command": "mkdir -p results && echo a > results/a.txt && "
                   "echo fig > plot.svg",
        "outputs": ["results/", "plot.svg"], "site": "local"})
    job = w.runner.wait(r["job_id"], 120)
    assert job["state"] == "DONE", job["error"]
    paths = {o["path"] for o in job["manifest"]["outputs"]}
    assert {"results/a.txt", "results/", "plot.svg"} <= paths
    tree = next(o for o in job["manifest"]["outputs"]
                if o["path"] == "results/")
    assert tree["preview"]["kind"] == "tree"


def test_missing_output_fails_with_honest_error(w):
    r = w.task_submit({"command": "true", "outputs": ["never.txt"],
                       "site": "local"})
    job = w.runner.wait(r["job_id"], 120)
    assert job["state"] == "FAILED"
    assert "was not produced" in job["error"]["detail"]
    assert "never.txt" in job["error"]["detail"]
