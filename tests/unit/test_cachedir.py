"""Node-local pixi cache resolution (user-model report: netfs-only
clusters — NFS home, BeeGFS scratch AND /tmp — break rattler's cache
locking; conda-pypi mapping fetch fails as 'Cache error: File still
doesn't exist')."""

import pytest

from weft import cachedir
from weft.cachedir import NETWORK_FS, local_cache_dir


def _mounts(monkeypatch, rows):
    monkeypatch.setattr(cachedir, "_mount_table",
                        lambda: sorted(rows, key=lambda r: len(r[0]),
                                       reverse=True))
    monkeypatch.setattr(cachedir.sys, "platform", "linux")


def test_ambient_pixi_cache_dir_is_respected(monkeypatch):
    monkeypatch.setenv("PIXI_CACHE_DIR", "/somewhere/mine")
    d, why = local_cache_dir()
    assert d is None and "respected" in why


def test_local_default_cache_stays(monkeypatch, tmp_path):
    monkeypatch.delenv("PIXI_CACHE_DIR", raising=False)
    _mounts(monkeypatch, [("/", "ext4")])
    d, why = local_cache_dir()
    assert d is None and "local" in why


def test_netfs_default_redirects_to_local(monkeypatch, tmp_path):
    """The cbe topology: home on NFS, /tmp on BeeGFS, XDG_RUNTIME_DIR
    on tmpfs — the resolver must land on the tmpfs candidate."""
    monkeypatch.delenv("PIXI_CACHE_DIR", raising=False)
    rt = tmp_path / "run"
    rt.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(rt))
    home = str(cachedir.Path.home())
    _mounts(monkeypatch, [
        ("/", "ext4"), (home, "nfs4"), ("/tmp", "beegfs"),
        (str(rt), "tmpfs"),
    ])
    d, why = local_cache_dir()
    assert d == str(rt / "weft-pixi-cache")
    assert "network fs" in why and "redirected" in why


def test_nothing_local_keeps_default_with_honest_why(monkeypatch, tmp_path):
    monkeypatch.delenv("PIXI_CACHE_DIR", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)
    home = str(cachedir.Path.home())
    _mounts(monkeypatch, [("/", "nfs"), (home, "nfs4"),
                          ("/tmp", "beegfs"), ("/dev/shm", "beegfs")])
    d, why = local_cache_dir()
    assert d is None and "no local candidate" in why


def test_unreadable_mount_table_keeps_default(monkeypatch):
    """Cannot tell = keep old behavior; the solve classifier names the
    lever if it then breaks. unknown ≠ netfs, unknown ≠ local claim."""
    monkeypatch.delenv("PIXI_CACHE_DIR", raising=False)
    _mounts(monkeypatch, [])
    d, why = local_cache_dir()
    assert d is None and "unverifiable" in why


def test_parallel_fs_types_are_network():
    for t in ("beegfs", "fhgfs", "lustre", "gpfs", "nfs4", "fuse.sshfs"):
        assert t in NETWORK_FS or t.split(".")[-1] in NETWORK_FS
