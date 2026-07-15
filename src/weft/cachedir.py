"""Node-local pixi/rattler cache resolution.

rattler's HTTP caches (conda-pypi mapping and friends) need file locking
that network filesystems routinely break — and on netfs-only clusters
even /tmp is remote (cbe.next: NFS home, BeeGFS scratch AND /tmp).
Solve caches are small (repodata + mappings), so volatility is fine;
correctness needs LOCAL. Never assume a path is local — read the mount
table. On macOS everything relevant is local.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# fs types where file locking / cache semantics are not to be trusted
NETWORK_FS = {
    "nfs", "nfs4", "beegfs", "fhgfs", "lustre", "gpfs", "cifs", "smbfs",
    "9p", "afs", "ceph", "fuse.beegfs", "fuse.sshfs", "fuse.gpfs",
}


def _mount_table() -> list[tuple[str, str]]:
    """[(mountpoint, fstype)] from /proc/mounts, longest paths first."""
    rows = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    rows.append((parts[1], parts[2]))
    except OSError:
        return []
    return sorted(rows, key=lambda r: len(r[0]), reverse=True)


def fs_type(path: Path) -> str | None:
    """Filesystem type at (the nearest existing parent of) path.
    None = cannot tell (no /proc/mounts)."""
    if sys.platform == "darwin":
        return "apfs"
    table = _mount_table()
    if not table:
        return None
    p = str(path.resolve()) if path.exists() else str(path)
    for mnt, typ in table:
        if p == mnt or p.startswith(mnt.rstrip("/") + "/") or mnt == "/":
            return typ
    return None


def is_local(path: Path) -> bool | None:
    """True/False when the mount table answers; None when it cannot."""
    t = fs_type(path)
    if t is None:
        return None
    return t.split(".")[-1] not in NETWORK_FS and t not in NETWORK_FS


def _default_pixi_cache() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "rattler" / "cache"
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "rattler" / "cache"


def _usable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".weft-probe"
        probe.write_bytes(b"1")
        probe.unlink()
        return True
    except OSError:
        return False


def local_cache_dir() -> tuple[str | None, str]:
    """-> (cache_dir or None, why). None = leave pixi's default alone.

    Resolution: ambient PIXI_CACHE_DIR is the user's explicit choice
    (respected untouched) → pixi's default location IF it sits on a
    local filesystem (persistent beats volatile; keeps cross-run
    repodata) → first usable genuinely-local of $XDG_RUNTIME_DIR,
    /dev/shm/weft-<uid>, $TMPDIR. When nothing can be verified local,
    keep the default and let the solve-error classifier name the lever.
    """
    if os.environ.get("PIXI_CACHE_DIR"):
        return None, "ambient PIXI_CACHE_DIR respected"
    default = _default_pixi_cache()
    if is_local(default) is not False:
        # local, or unverifiable: keep persistence (old behavior)
        return None, "default cache is local (or unverifiable)"
    candidates = []
    if os.environ.get("XDG_RUNTIME_DIR"):
        candidates.append(Path(os.environ["XDG_RUNTIME_DIR"])
                          / "weft-pixi-cache")
    candidates.append(Path(f"/dev/shm/weft-{os.getuid()}") / "pixi-cache")
    if os.environ.get("TMPDIR"):
        candidates.append(Path(os.environ["TMPDIR"])
                          / f"weft-{os.getuid()}-pixi-cache")
    for cand in candidates:
        if is_local(cand.parent if not cand.exists() else cand) \
                and _usable(cand):
            return str(cand), (f"default cache is on "
                               f"{fs_type(default)!r} (network fs) — "
                               f"redirected to node-local storage")
    return None, "default cache is on a network fs but no local " \
                 "candidate found — solves with pypi deps may fail"
