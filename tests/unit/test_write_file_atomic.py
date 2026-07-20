"""bug2 (aba handoff): the kernel driver treats `.code`-file existence as
completeness, so the controller's write_file must publish ATOMICALLY.
The old shapes — `cat > dest` (truncates at redirect-open, bytes arrive
later over ssh) and Path.write_bytes (truncate then fill) — let the
driver exec an empty or PARTIAL block and report rc=0: silent false
success, side effects lost. Fix: tmp sibling + verify + rename, the same
contract the driver already keeps for its own `.rc`."""

import hashlib
import re
import threading
from pathlib import Path

import pytest

from weft.adapters.base import ShimResult
from weft.adapters.local import LocalAdapter
from weft.adapters.ssh import SSHAdapter
from weft.errors import WeftError


# -- ssh: the published command stages, verifies, renames ------------------

def _adapter(**kw):
    return SSHAdapter("s", "target", "/site/root", user="u", **kw)


def _capture(monkeypatch, ad, rc=0, err=""):
    seen = {}

    def fake_run(cmd, *, input_bytes=None, timeout=120.0):
        seen["cmd"], seen["stdin"] = cmd, input_bytes
        return ShimResult(rc, "", err)

    monkeypatch.setattr(ad, "_run", fake_run)
    return seen


def test_ssh_write_file_publishes_via_tmp_verify_rename(monkeypatch):
    ad = _adapter()
    seen = _capture(monkeypatch, ad)
    data = b"print('blk-4 ok', 4*7)\n"
    ad.write_file("jobs/j1/blocks/0004.code", data)

    cmd, dest = seen["cmd"], "/site/root/jobs/j1/blocks/0004.code"
    assert seen["stdin"] == data
    # the cat redirect must target a tmp SIBLING, never dest itself
    m = re.search(r"cat > (\S+)", cmd)
    assert m and m.group(1).startswith(dest + ".wtmp."), cmd
    tmp = m.group(1)
    # bytes verified against the controller-side digest, permissioned,
    # then atomically renamed into place (same dir -> same filesystem)
    assert hashlib.sha256(data).hexdigest() in cmd
    assert "sha256sum -c -" in cmd
    assert f"mv -f {tmp} {dest}" in cmd
    assert cmd.index(f"chmod 644 {tmp}") < cmd.index("mv -f")
    # any failure removes the staged tmp — no litter in polled dirs
    assert f"rm -f {tmp}" in cmd


def test_ssh_write_file_shared_root_group_bit(monkeypatch):
    ad = _adapter(shared=True)
    seen = _capture(monkeypatch, ad)
    ad.write_file("f", b"x")
    assert "chmod 664" in seen["cmd"]


def test_ssh_write_file_failure_is_honest(monkeypatch):
    ad = _adapter()
    _capture(monkeypatch, ad, rc=1, err="sha256sum: WARNING: 1 computed checksum did NOT match")
    with pytest.raises(WeftError) as ei:
        ad.write_file("f", b"x")
    assert ei.value.code == "site.unreachable" and ei.value.retryable


# -- local: existence == completeness, always ------------------------------

def test_local_write_file_atomic_visibility(tmp_path):
    """A poller that reads the instant the path appears must ALWAYS see
    the complete payload — the exact contract the kernel driver's
    exists->read loop assumes."""
    ad = LocalAdapter("l", tmp_path)
    payload = b"x" * (1 << 21)  # 2 MiB: widen any truncate-then-fill window
    p = tmp_path / "blocks" / "0000.code"
    caught = []

    for _ in range(20):
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
        t.join(5)

    assert len(caught) == 20
    assert all(c == payload for c in caught)
    assert list((tmp_path / "blocks").glob("*.wtmp.*")) == []


def test_local_write_file_mode_content_shared_bit(tmp_path):
    ad = LocalAdapter("l", tmp_path, shared=True)
    ad.write_file("d/x.sh", b"echo hi\n", mode=0o755)
    p = tmp_path / "d" / "x.sh"
    assert p.read_bytes() == b"echo hi\n"
    assert (p.stat().st_mode & 0o777) == 0o775


def test_local_write_file_cleans_tmp_on_failure(tmp_path, monkeypatch):
    ad = LocalAdapter("l", tmp_path)

    def boom(self, target):
        raise OSError("no rename")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError):
        ad.write_file("f.txt", b"data")
    assert not (tmp_path / "f.txt").exists()
    assert list(tmp_path.glob("*.wtmp.*")) == []
