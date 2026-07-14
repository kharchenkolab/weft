"""Squashfs strategy selection: pure decision-table cases (design:
misc/sqaush.md; measured inputs: clip + cbe.next, 2026-07-14)."""

import pytest

from weft.capability import squashfs_mode
from weft.errors import WeftError
from weft.strategy import select_strategy


def _caps(root_fs="fhgfs", userns=True, squashfuse="/usr/libexec/apptainer/bin/squashfuse_ll",
          mksquashfs="/usr/sbin/mksquashfs", dev_fuse=True, internet=True):
    return {
        "os": "linux", "arch": "x86_64", "cpus": 8, "mem_gb": 32,
        "internet": internet, "runtimes": {"apptainer": "1.4.5", "docker": False},
        "scheduler": {"type": "slurm"}, "module_system": True,
        "gpus": [], "cuda_driver": "", "storage": {},
        "squashfs": {"dev_fuse": dev_fuse, "squashfuse": squashfuse,
                     "mksquashfs": mksquashfs, "userns": userns,
                     "root_fs": root_fs},
    }


def test_auto_on_parallel_fs_with_userns():
    assert select_strategy(_caps()) == "squashfs"
    assert select_strategy(_caps(), modules=["gcc/13"]) == "modules+squashfs"


def test_beegfs_root_without_userns_falls_back():
    """fusermount3 refuses to mount over BeeGFS; without namespaces the
    site cannot mount at the canonical path — honest fallback."""
    caps = _caps(userns=False)
    assert squashfs_mode(caps) is None
    assert select_strategy(caps) == "prefix"


def test_nfs_root_defaults_to_prefix_but_prefers_squashfs():
    caps = _caps(root_fs="nfs", userns=False)   # direct mounts allowed on nfs
    assert squashfs_mode(caps) == "direct"
    assert select_strategy(caps) == "prefix"    # no recurring parallel-FS pain
    assert select_strategy(caps, prefer="squashfs") == "squashfs"


def test_prefer_refused_without_tooling():
    caps = _caps(squashfuse="", root_fs="nfs")
    with pytest.raises(WeftError) as e:
        select_strategy(caps, prefer="squashfs")
    assert e.value.code == "env.unsatisfiable_on_site"
    assert select_strategy(caps) == "prefix"    # auto path stays honest


def test_missing_mksquashfs_disables_v1():
    caps = _caps(mksquashfs="")                 # clip login-node shape
    assert squashfs_mode(caps) is None
    assert select_strategy(caps) == "prefix"


def test_userns_mode_wins_over_direct():
    assert squashfs_mode(_caps(root_fs="nfs")) == "userns"


def test_no_internet_parallel_fs_still_squashfs():
    """The builder can unpack a packed blob and squash it — tooling, not
    network, is the gate."""
    assert select_strategy(_caps(internet=False)) == "squashfs"
