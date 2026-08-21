"""Batched tree walks (aba2 perf note): list-tree and hash-tree used a
per-file fork loop — two forks per file for stat, four for hash rows —
measured at 2.6s/8.4s for 930 files where one batched pass does 0.14s/
0.25s. Every run-card open (run_inventory live=True), every retain
scan, and every output collection paid it. The batch must be
INVISIBLE: same rows, same order, same truncation contract — these
tests hold the conformance against a python-computed oracle and
against the preserved per-file path (WEFT_TREE_BATCH=0), plus the one
deliberate semantics change: a TAB-carrying name used to garble its
TSV row (killing the consumer's int() parse); now it is excluded and
counted loudly on stderr as '#skipped_malformed N'."""

import hashlib
import os
import subprocess
import time
from pathlib import Path

import pytest

SHIM = str(Path(__file__).resolve().parents[2]
           / "src" / "weft" / "shim" / "weft-shim")


def _shim(*argv, env_extra=None):
    env = {**os.environ, "LC_ALL": "C", **(env_extra or {})}
    r = subprocess.run(["sh", SHIM, *argv], capture_output=True,
                       text=True, env=env, timeout=300)
    return r


def _corpus(root: Path):
    (root / "deep/nested/dir").mkdir(parents=True)
    (root / "empty-dir").mkdir()
    (root / "plain.txt").write_text("plain")
    (root / "name with space.txt").write_text("spaced-content")
    (root / "deep/nested/dir/leaf.bin").write_bytes(b"\x00\x01leaf")
    (root / "runner").write_text("#!/bin/sh\n")
    (root / "runner").chmod(0o755)
    os.symlink("plain.txt", root / "alink")
    return {"plain.txt", "name with space.txt",
            "deep/nested/dir/leaf.bin", "runner"}


def _truth(root: Path, files):
    out = {}
    for rel in files:
        st = os.lstat(root / rel)
        out[rel] = (st.st_size, int(st.st_mtime))
    return out


def test_list_tree_conformance_against_stat_truth(tmp_path):
    files = _corpus(tmp_path)
    r = _shim("list-tree", "--root", str(tmp_path), "--max", "100000")
    assert r.returncode == 0, r.stderr
    rows = [ln.split("\t") for ln in r.stdout.splitlines()]
    # 4-field rows (trailing empty hash column), sorted, symlink and
    # empty dir absent
    assert [c[0] for c in rows] == sorted(files)
    truth = _truth(tmp_path, files)
    for c in rows:
        assert len(c) == 4 and c[3] == "", c
        assert (int(c[1]), int(c[2])) == truth[c[0]], c
    assert f"#total {len(files)}" in r.stderr


def test_list_tree_tab_name_excluded_loudly(tmp_path):
    """The deliberate semantics change: a tab-carrying name cannot
    ride a TSV row — it used to garble it (ValueError in the scan
    consumer); now the row is excluded and COUNTED."""
    files = _corpus(tmp_path)
    (tmp_path / "bad\tname.txt").write_text("x")
    r = _shim("list-tree", "--root", str(tmp_path), "--max", "100000")
    assert r.returncode == 0, r.stderr
    paths = [ln.split("\t")[0] for ln in r.stdout.splitlines()]
    assert paths == sorted(files)              # clean rows only
    assert "#skipped_malformed 1" in r.stderr
    assert f"#total {len(files) + 1}" in r.stderr   # honest full count


def test_list_tree_cap_is_first_n_sorted(tmp_path):
    for i in range(9):
        (tmp_path / f"f{i}.txt").write_text(str(i))
    r = _shim("list-tree", "--root", str(tmp_path), "--max", "4")
    paths = [ln.split("\t")[0] for ln in r.stdout.splitlines()]
    assert paths == [f"f{i}.txt" for i in range(4)]
    assert "#total 9" in r.stderr


def test_list_tree_hash_under_lane_still_hashes(tmp_path):
    (tmp_path / "small.txt").write_text("tiny")
    (tmp_path / "big.bin").write_bytes(b"x" * 4096)
    r = _shim("list-tree", "--root", str(tmp_path),
              "--hash-under", "1024", "--max", "100")
    rows = {c[0]: c for c in
            (ln.split("\t") for ln in r.stdout.splitlines())}
    assert rows["small.txt"][3] == hashlib.sha256(b"tiny").hexdigest()
    assert rows["big.bin"][3] == ""            # over threshold: stat only


def test_list_tree_file_root_unchanged(tmp_path):
    f = tmp_path / "single.dat"
    f.write_text("solo")
    r = _shim("list-tree", "--root", str(f))
    (row,) = r.stdout.splitlines()
    c = row.split("\t")
    assert c[0] == "." and int(c[1]) == 4
    assert "#total 1" in r.stderr


def test_hash_tree_identity_conformance(tmp_path):
    """Identity-critical: these rows mint tree refs. Batched output
    must match the python oracle AND the preserved per-file path
    byte-for-byte — a zip misalignment attributing one file's hash to
    another's path would fork identity silently."""
    files = _corpus(tmp_path)
    fast = _shim("hash-tree", "--root", str(tmp_path))
    slow = _shim("hash-tree", "--root", str(tmp_path),
                 env_extra={"WEFT_TREE_BATCH": "0"})
    assert fast.returncode == 0 and slow.returncode == 0
    assert fast.stdout == slow.stdout          # lever == old loop
    rows = {c[1]: c for c in
            (ln.split("\t") for ln in fast.stdout.splitlines())}
    assert set(rows) == files | {"alink"}
    assert [c[1] for c in
            (ln.split("\t") for ln in fast.stdout.splitlines())] == \
        sorted(files | {"alink"})              # combined sorted order
    for rel in files:
        kind, _, x, sz, h = rows[rel]
        assert kind == "file"
        assert int(x) == (1 if rel == "runner" else 0)
        assert int(sz) == os.stat(tmp_path / rel).st_size
        assert h == hashlib.sha256(
            (tmp_path / rel).read_bytes()).hexdigest()
    kind, _, x, sz, target = rows["alink"]
    assert kind == "link" and target == "plain.txt"


def test_tree_walk_cost_budget(tmp_path):
    """The cost pin (reality-asserts-budgets doctrine): 1000 files under
    2s. The per-file loop measured 2.6s+ for 930 — a regression to
    per-file forks goes red here; the batch does it in ~0.15s."""
    for d in range(25):
        (tmp_path / f"d{d}").mkdir()
        for f in range(40):
            (tmp_path / f"d{d}" / f"f{f}.txt").write_text(f"{d}-{f}")
    t0 = time.monotonic()
    r = _shim("list-tree", "--root", str(tmp_path), "--max", "100000")
    lt = time.monotonic() - t0
    assert len(r.stdout.splitlines()) == 1000
    t0 = time.monotonic()
    r = _shim("hash-tree", "--root", str(tmp_path))
    ht = time.monotonic() - t0
    assert len(r.stdout.splitlines()) == 1000
    print(f"\nlist-tree 1000 files: {lt:.2f}s; hash-tree: {ht:.2f}s")
    assert lt < 2.0, f"list-tree took {lt:.2f}s — per-file forks are back?"
    assert ht < 4.0, f"hash-tree took {ht:.2f}s — per-file forks are back?"
