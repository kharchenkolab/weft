"""Retention R1 (misc/retention.md): the terminal inventory — a
stat-only receipt of what a run left behind, recorded at terminal
state, surviving everything that later deletes bytes."""

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_bin": pixi_bin,
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def test_shim_list_tree_stats_without_hashing(w, tmp_path):
    adapter = w.adapters["local"]
    d = tmp_path / "tree"
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_text("alpha")
    (d / "sub" / "b.bin").write_bytes(b"\x00" * 2048)
    r = adapter.shim(["list-tree", "--root", str(d)])
    assert r.rc == 0
    rows = {line.split("\t")[0]: line.split("\t")
            for line in r.out.splitlines()}
    assert rows["a.txt"][1] == "5" and rows["a.txt"][3] == ""
    assert rows["sub/b.bin"][1] == "2048"
    assert int(rows["a.txt"][2]) > 0                    # mtime
    assert "#total 2" in r.err
    # opt-in hashing by SIZE THRESHOLD: small hashed, large not
    r = adapter.shim(["list-tree", "--root", str(d),
                      "--hash-under", "1000"])
    rows = {line.split("\t")[0]: line.split("\t")
            for line in r.out.splitlines()}
    assert len(rows["a.txt"][3]) == 64
    assert rows["sub/b.bin"][3] == ""
    # budget honesty
    r = adapter.shim(["list-tree", "--root", str(d), "--max", "1"])
    assert len(r.out.splitlines()) == 1 and "#total 2" in r.err


def test_job_inventory_recorded_at_terminal_both_ways(w):
    # DONE: declared output + undeclared residue both in the receipt
    r = w.task_submit({"command": "echo keep > results/out.txt; "
                                  "echo scratch > residue.dat",
                       "outputs": ["results/"], "site": "local"})
    assert w.runner.wait(r["job_id"], 120)["state"] == "DONE"
    inv = w.run_inventory(r["job_id"])
    paths = {e["path"] for e in inv["entries"]}
    assert "results/out.txt" in paths and "residue.dat" in paths
    assert all("sha256" not in e for e in inv["entries"])  # stat-only
    assert inv["truncated"] is False

    # FAILED runs get receipts too — failure residue is often the point
    r2 = w.task_submit({"command": "echo partial > half.dat; exit 3",
                        "site": "local"})
    assert w.runner.wait(r2["job_id"], 120)["state"] == "FAILED"
    inv2 = w.run_inventory(r2["job_id"])
    assert "half.dat" in {e["path"] for e in inv2["entries"]}

    # filters at read
    only = w.run_inventory(r["job_id"], glob="results/*")
    assert {e["path"] for e in only["entries"]} == {"results/out.txt"}


def test_kernel_inventory_on_stop_and_survival(w, tmp_path):
    k = w.kernel_start("local", "python")["kernel_id"]
    r = w.kernel_exec(
        k, "import os\n"
           "open(os.environ['WEFT_BLOCK_DIR'] + '/fig.svg', 'w')"
           ".write('<svg/>')", timeout=60)
    assert r["rc"] == 0
    w.kernel_stop(k)
    inv = w.run_inventory(k)
    paths = {e["path"] for e in inv["entries"]}
    assert any(p.endswith("fig.svg") for p in paths), paths

    # KNOWLEDGE outlives the sandbox: delete the jobdir, receipt stays
    jobdir = w.store.get_kernel(k)["jobdir"]
    w.adapters["local"].run_cmd(
        f"rm -rf {w.adapters['local'].path(jobdir)}")
    inv2 = w.run_inventory(k)
    assert {e["path"] for e in inv2["entries"]} == paths


def test_missing_inventory_is_honest(w):
    r = w.run_inventory("jb_never_ran")
    assert r["error"] == "data.missing"
    assert "terminal" in r["hints"]["note"]
