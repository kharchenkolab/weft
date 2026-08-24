"""Read-only zero-copy views (Ask 26 + user ruling, amended): fetching
or staging already-local bytes must not COPY them — pipelines get
read-only views (hardlink when the blob's inode is safe to share, CoW
clone when it isn't, byte copy only where the filesystem forces it).
The amendment matters: registration HARDLINK-ingests, so many blobs
share an inode with the USER'S ORIGINAL file — those are never
chmod'd, never handed out writable; the clone's independence is what
keeps the original untouched. The chmod loophole (an owner can always
chmod+write) is accepted per the ruling — the verify fence DETECTS
falsification at next use, and that detection is pinned here."""

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from weft.api import Weft
from weft.cas import place_view

SHIM = str(Path(__file__).resolve().parents[2]
           / "src" / "weft" / "shim" / "weft-shim")


@pytest.fixture
def w(tmp_path, pixi_bin):
    return Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")


def _mode(p):
    return os.stat(p).st_mode & 0o777


def test_weft_owned_blob_converges_and_hardlinks(w, tmp_path):
    """A blob weft owns alone (put_bytes: nlink==1, writable) is
    converged to 0444 and the fetch is a HARDLINK — zero bytes."""
    ref = w.cas.put_bytes(b"owned-bytes").ref
    blob = w.cas.open_blob(ref)
    assert os.stat(blob).st_nlink == 1
    dest = tmp_path / "view.bin"
    out = w.data_fetch(ref, str(dest))
    assert out["methods"] == {"hardlink": 1} and out["read_only"]
    assert os.stat(dest).st_ino == os.stat(blob).st_ino   # zero bytes
    assert _mode(dest) == 0o444 and _mode(blob) == 0o444  # converged


def test_shared_inode_blob_clones_and_original_untouched(w, tmp_path):
    """The observed case: a registered local dataset (hardlink ingest —
    blob shares the USER'S inode). The fetch must not copy bytes on a
    CoW fs, must land read-only, and must leave the original's mode,
    content, and inode alone."""
    orig = tmp_path / "dataset.bin"
    orig.write_bytes(b"D" * 4096)
    os.chmod(orig, 0o644)
    ref = w.data_register(str(orig))["ref"]
    blob = w.cas.open_blob(ref)
    assert os.stat(blob).st_nlink >= 2                    # shared inode
    dest = tmp_path / "sandbox" / "dataset.bin"
    out = w.data_fetch(ref, str(dest))
    (method,) = out["methods"]
    if sys.platform == "darwin":                          # APFS clones
        assert method == "reflink", out
    else:
        assert method in ("reflink", "copy")
    assert os.stat(dest).st_ino != os.stat(orig).st_ino   # independent
    assert _mode(dest) in (0o444,)
    assert _mode(orig) == 0o644                           # untouched
    assert orig.read_bytes() == b"D" * 4096
    assert os.stat(blob).st_nlink >= 2                    # not unlinked


def test_writable_fetch_is_independent(w, tmp_path):
    ref = w.cas.put_bytes(b"scratch-me").ref
    dest = tmp_path / "scratch.bin"
    out = w.data_fetch(ref, str(dest), writable=True)
    (method,) = out["methods"]
    assert method in ("reflink", "copy")                  # NEVER hardlink
    assert not out["read_only"]
    dest.write_bytes(b"scribbled")                        # allowed
    assert w.cas.verify(ref)                              # blob unharmed


def test_readonly_view_refuses_writes(w, tmp_path):
    ref = w.cas.put_bytes(b"immutable").ref
    dest = tmp_path / "ro.bin"
    w.data_fetch(ref, str(dest))
    with pytest.raises(PermissionError):
        open(dest, "w")
    with pytest.raises(PermissionError):
        open(dest, "r+")
    dest.unlink()                                         # deletion works


def test_chmod_loophole_is_detected_at_next_use(w, tmp_path):
    """Permissions guard accidents, not determination — but the verify
    fence turns a chmod+write falsification into data.verify_failed at
    the next fetch, never a silent wrong-bytes success."""
    ref = w.cas.put_bytes(b"trusted-content").ref
    dest = tmp_path / "view.bin"
    w.data_fetch(ref, str(dest))                          # hardlink view
    os.chmod(dest, 0o644)
    dest.write_bytes(b"falsified!")                       # hits the inode
    bad = w.data_fetch(ref, str(tmp_path / "again.bin"))
    assert bad["error"] == "data.verify_failed"


def test_tree_exec_and_symlink_semantics(w, tmp_path):
    src = tmp_path / "pkg"
    src.mkdir()
    (src / "data.txt").write_text("plain")
    (src / "run.sh").write_text("#!/bin/sh\necho ran\n")
    os.chmod(src / "run.sh", 0o755)
    os.symlink("data.txt", src / "alias")
    ref = w.data_register(str(src))["ref"]
    dest = tmp_path / "out"
    out = w.data_fetch(ref, str(dest))
    m = out["methods"]
    assert m.get("symlink") == 1
    # exec is a materialize-time property: 0555, own inode, still runs
    assert _mode(dest / "run.sh") == 0o555
    assert os.stat(dest / "run.sh").st_ino != \
        os.stat(src / "run.sh").st_ino
    r = subprocess.run([str(dest / "run.sh")], capture_output=True,
                       text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ran"
    assert _mode(dest / "data.txt") == 0o444
    assert (dest / "alias").is_symlink()
    # re-fetch over the existing dest: idempotent
    out2 = w.data_fetch(ref, str(dest))
    assert "error" not in out2


def test_place_view_cross_device_falls_to_copy(tmp_path, monkeypatch):
    """os.link and clone both refused => byte copy, still read-only —
    the fallback is honest, never a failure."""
    src = tmp_path / "blob"
    src.write_bytes(b"x" * 64)
    os.chmod(src, 0o444)
    import weft.cas as cas_mod
    monkeypatch.setattr(cas_mod.os, "link",
                        lambda *a: (_ for _ in ()).throw(OSError("EXDEV")))
    monkeypatch.setattr(cas_mod, "_clone", lambda s, d: False)
    dst = tmp_path / "view"
    assert place_view(src, dst) == "copy"
    assert dst.read_bytes() == b"x" * 64 and _mode(dst) == 0o444


def test_shim_materialize_stages_readonly_views(w, tmp_path):
    """The staging lane (every task/kernel input on every adapter goes
    through shim materialize): same decision tree, site-side."""
    cas = tmp_path / "scas"
    jobdir = tmp_path / "job"
    jobdir.mkdir()
    tab = "\t"

    def blob(content):
        h = hashlib.sha256(content).hexdigest()
        p = cas / h[:2] / h
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return h, p

    ro_h, ro_p = blob(b"ro-blob")
    os.chmod(ro_p, 0o444)
    sole_h, sole_p = blob(b"sole-writable")          # nlink 1, 0644
    shared_h, shared_p = blob(b"shared-writable")
    user_file = tmp_path / "users-own.bin"
    os.link(shared_p, user_file)                     # simulate ingest
    exec_h, exec_p = blob(b"#!/bin/sh\necho hi\n")
    plan = tmp_path / "plan.tsv"
    plan.write_text(
        f"a/ro.bin{tab}{ro_h}{tab}0\n"
        f"b/sole.bin{tab}{sole_h}{tab}0\n"
        f"c/shared.bin{tab}{shared_h}{tab}0\n"
        f"d/tool.sh{tab}{exec_h}{tab}1\n"
        f"e/lnk{tab}a/ro.bin{tab}L\n")
    r = subprocess.run(
        ["sh", SHIM, "materialize", "--cas", str(cas), "--dir",
         str(jobdir), "--plan", str(plan)],
        capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"})
    assert r.returncode == 0, r.stderr
    # ro blob: hardlinked (zero bytes), stays 0444
    assert os.stat(jobdir / "a/ro.bin").st_ino == os.stat(ro_p).st_ino
    assert _mode(jobdir / "a/ro.bin") == 0o444
    # sole writable blob: converged then hardlinked
    assert _mode(sole_p) == 0o444
    assert os.stat(jobdir / "b/sole.bin").st_ino == os.stat(sole_p).st_ino
    # shared-inode blob: view has its OWN inode; user's file untouched
    assert os.stat(jobdir / "c/shared.bin").st_ino != \
        os.stat(shared_p).st_ino
    assert _mode(jobdir / "c/shared.bin") == 0o444
    assert _mode(user_file) & 0o200                  # still writable
    assert user_file.read_bytes() == b"shared-writable"
    # exec: own inode, 0555, runs
    assert _mode(jobdir / "d/tool.sh") == 0o555
    assert os.stat(jobdir / "d/tool.sh").st_ino != os.stat(exec_p).st_ino
    assert (jobdir / "e/lnk").is_symlink()


def test_task_inputs_land_readonly_end_to_end(w, tmp_path, pixi_bin):
    """The whole path a pipeline actually takes: register -> submit
    with inputs -> the sandbox mount is read-only; reads succeed and
    an in-place write fails inside the task."""
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    data = tmp_path / "input.csv"
    data.write_text("a,b\n1,2\n")
    ref = w.data_register(str(data))["ref"]
    r = w.task_submit({
        "command": "cat in.csv > readable.txt; "
                   "if echo x >> in.csv 2>/dev/null; then "
                   "echo WROTE > verdict.txt; else "
                   "echo REFUSED > verdict.txt; fi",
        "inputs": [{"ref": ref, "mount_as": "in.csv"}],
        "outputs": ["readable.txt", "verdict.txt"], "site": "local"})
    assert w.runner.wait(r["job_id"], 120)["state"] == "DONE"
    jobdir = Path(w.adapters["local"].path(f"jobs/{r['job_id']}"))
    assert (jobdir / "verdict.txt").read_text().strip() == "REFUSED"
    assert (jobdir / "readable.txt").read_text() == "a,b\n1,2\n"
    assert data.read_text() == "a,b\n1,2\n"          # original intact


def test_zero_copy_cost_pin(w, tmp_path):
    """The observed waste, pinned: a 64 MB already-local fetch must
    move ZERO bytes (inode identity) and return fast — the full byte
    copy this replaces is the red case. (Registered then original
    removed: the blob becomes weft's sole link => converge+hardlink,
    the steady-state lane.)"""
    big = tmp_path / "big-src.bin"
    big.write_bytes(os.urandom(1 << 20) * 64)
    ref = w.data_register(str(big))["ref"]
    big.unlink()                                  # weft owns the blob now
    dest = tmp_path / "big.bin"
    t0 = time.monotonic()
    out = w.data_fetch(ref, str(dest))
    dt = time.monotonic() - t0
    assert out["methods"] == {"hardlink": 1}, out
    assert os.stat(dest).st_ino == os.stat(w.cas.open_blob(ref)).st_ino
    assert dt < 1.0, f"64MB local fetch took {dt:.2f}s — copying again?"
