"""Local content-addressed store and DataRef registration (doc 04 §2).

The workspace stays a normal directory tree; the CAS holds hardlinks so
content gets a stable identity without duplicating bytes. Trees are stored
as canonical manifests referencing file blobs. An mtime+size fast path
avoids rehashing unchanged workspace files.

Layout: <cas_root>/<sha256[:2]>/<sha256>            (file blobs)
        <cas_root>/trees/<sha256>.json              (tree manifests)
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import uuid

from .errors import WeftError
from .ids import (DREF_SCHEME, _is_exec, canonical_json, data_ref, hash_file,
                  hash_tree, sha256_bytes)


def place_blob(src: Path, dst: Path) -> None:
    """Hardlink-or-copy src to dst atomically, safely under concurrency.

    The tmp name must be unique per attempt: with a shared `<dst>.tmp`,
    two stagers of the same blob raced — the loser's os.link hit EEXIST,
    fell back to copy2, and copy2 refused to copy src onto its own fresh
    hardlink (SameFileError; found in the wild by weft-ui seeding a
    24-element array that mounts one ref). With unique names the racers
    are independent and os.replace makes the last one a harmless no-op.
    """
    tmp = dst.with_name(f"{dst.name}.tmp.{uuid.uuid4().hex[:8]}")
    try:
        try:
            os.link(src, tmp)  # hardlink: free and instant
        except OSError:
            shutil.copy2(src, tmp)  # cross-device fallback
        os.replace(tmp, dst)
    finally:
        # rename(2) is a successful NO-OP when tmp and dst are hardlinks
        # of the same inode (exactly what concurrent stagers produce) —
        # the tmp survives it. Consume it ourselves; also covers the
        # exception path.
        tmp.unlink(missing_ok=True)


@dataclass
class DataRefInfo:
    ref: str
    kind: str  # "file" | "tree"
    bytes: int
    chunks: list[str] | None = None
    exec: bool = False  # file refs only; trees carry per-entry exec bits
    plain_sha256: str | None = None  # content hash when name is a merkle root


class LocalCAS:
    def __init__(self, root: Path):
        self.root = Path(root)
        (self.root / "trees").mkdir(parents=True, exist_ok=True)
        self._fast_path_index: dict[str, tuple[float, int, str]] = {}
        self._index_file = self.root / "fastpath.json"
        if self._index_file.exists():
            try:
                self._fast_path_index = {
                    k: tuple(v) for k, v in json.loads(self._index_file.read_text()).items()
                }
            except (json.JSONDecodeError, TypeError, ValueError):
                self._fast_path_index = {}

    # -- registration -------------------------------------------------------

    def _blob_path(self, hexdigest: str) -> Path:
        return self.root / hexdigest[:2] / hexdigest

    def _store_blob(self, src: Path, hexdigest: str) -> None:
        dst = self._blob_path(hexdigest)
        if dst.exists():
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        place_blob(src, dst)

    # A file modified less than this long ago is never trusted to the fast
    # path (git's "racily clean" rule): a same-size rewrite inside timestamp
    # granularity would otherwise return a stale hash.
    _SETTLE_S = 2.0

    def register_file(self, path: Path) -> DataRefInfo:
        path = Path(path).resolve()
        if not path.is_file():
            raise WeftError("data.missing", f"not a file: {path}", stage="staging")
        st = path.stat()
        cached = self._fast_path_index.get(str(path))
        settled = (time.time() - st.st_mtime) > self._SETTLE_S
        if (
            cached
            and cached[0] == st.st_mtime_ns
            and cached[1] == st.st_size
            and cached[2] == st.st_ino
            and settled
        ):
            hexdigest = cached[3]
            if self._blob_path(hexdigest).exists():
                return DataRefInfo(data_ref(hexdigest), "file", st.st_size,
                                   exec=_is_exec(st.st_mode))
        digest = hash_file(path)
        self._store_blob(path, digest.sha256)
        self._fast_path_index[str(path)] = (st.st_mtime_ns, st.st_size, st.st_ino, digest.sha256)
        self._save_index()
        return DataRefInfo(data_ref(digest.sha256), "file", digest.size,
                           digest.chunks, exec=_is_exec(st.st_mode),
                           plain_sha256=digest.plain_sha256)

    def register_tree(self, path: Path) -> DataRefInfo:
        path = Path(path).resolve()
        if not path.is_dir():
            raise WeftError("data.missing", f"not a directory: {path}", stage="staging")
        tree_hash, manifest = hash_tree(path)
        total = 0
        for entry in manifest:
            if entry["kind"] == "file":
                self._store_blob(path / entry["path"], entry["sha256"])
                total += entry["size"]
        (self.root / "trees" / f"{tree_hash}.json").write_text(
            json.dumps(manifest, indent=None, sort_keys=True)
        )
        return DataRefInfo(data_ref(tree_hash), "tree", total)

    def register(self, path: Path) -> DataRefInfo:
        path = Path(path)
        return self.register_tree(path) if path.is_dir() else self.register_file(path)

    def put_bytes(self, data: bytes) -> DataRefInfo:
        hexdigest = sha256_bytes(data)
        dst = self._blob_path(hexdigest)
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            # unique tmp: concurrent writers of the same content must not
            # share a scratch name (the loser's replace would ENOENT)
            tmp = dst.with_name(f"{dst.name}.tmp.{uuid.uuid4().hex[:8]}")
            tmp.write_bytes(data)
            os.replace(tmp, dst)
        return DataRefInfo(data_ref(hexdigest), "file", len(data))

    # -- retrieval ----------------------------------------------------------

    def _hex(self, ref: str) -> str:
        if not ref.startswith(DREF_SCHEME):
            raise WeftError("data.missing", f"not a DataRef: {ref}", stage="staging")
        return ref[len(DREF_SCHEME):]

    def kind_of(self, ref: str) -> str | None:
        hexdigest = self._hex(ref)
        if (self.root / "trees" / f"{hexdigest}.json").exists():
            return "tree"
        if self._blob_path(hexdigest).exists():
            return "file"
        return None

    def open_blob(self, ref: str) -> Path:
        p = self._blob_path(self._hex(ref))
        if not p.exists():
            raise WeftError("data.missing", f"blob not in local CAS: {ref}", stage="staging")
        return p

    def put_tree_manifest(self, tree_hash: str, manifest: list[dict]) -> None:
        """Adopt a tree manifest computed elsewhere (e.g. site-side hash-tree)."""
        (self.root / "trees" / f"{tree_hash}.json").write_text(
            json.dumps(manifest, indent=None, sort_keys=True)
        )

    def tree_manifest(self, ref: str) -> list[dict]:
        p = self.root / "trees" / f"{self._hex(ref)}.json"
        if not p.exists():
            raise WeftError("data.missing", f"tree not in local CAS: {ref}", stage="staging")
        return json.loads(p.read_text())

    def materialize(self, ref: str, dest: Path, mode: str = "hardlink") -> None:
        """Place ref's content at dest (hardlink when possible, else copy)."""
        kind = self.kind_of(ref)
        if kind is None:
            raise WeftError("data.missing", f"unknown ref: {ref}", stage="staging")
        if kind == "file":
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._place(self.open_blob(ref), dest, mode)
            return
        for entry in self.tree_manifest(ref):
            target = dest / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry["kind"] == "link":
                if target.is_symlink() or target.exists():
                    target.unlink()
                os.symlink(entry["target"], target)
                continue
            # exec files are copied: chmod on a hardlink would mutate the CAS inode
            self._place(
                self._blob_path(entry["sha256"]), target,
                "copy" if entry.get("exec") else mode,
            )
            if entry.get("exec"):
                os.chmod(target, os.stat(target).st_mode | 0o755)

    @staticmethod
    def _place(src: Path, dst: Path, mode: str) -> None:
        if dst.exists():
            dst.unlink()
        if mode == "hardlink":
            try:
                os.link(src, dst)
                return
            except OSError:
                pass
        shutil.copy2(src, dst)

    def verify(self, ref: str) -> bool:
        """Re-hash stored content against its address (corruption check)."""
        kind = self.kind_of(ref)
        hexdigest = self._hex(ref)
        if kind == "file":
            return hash_file(self._blob_path(hexdigest)).sha256 == hexdigest
        if kind == "tree":
            manifest = self.tree_manifest(ref)
            return sha256_bytes(canonical_json(manifest)) == hexdigest
        return False

    def _save_index(self) -> None:
        tmp = self._index_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._fast_path_index))
        os.replace(tmp, self._index_file)


# -- staging plan arithmetic (pure; doc 04 §3) -------------------------------

@dataclass
class StagingPlan:
    to_transfer: list[str]     # refs missing at the target
    present: list[str]         # refs already there
    bytes_to_move: int

    def to_dict(self) -> dict:
        return {
            "transfer": self.to_transfer,
            "already_present": self.present,
            "bytes_to_move": self.bytes_to_move,
        }


def staging_plan(
    required: list[str],
    present_at_target: set[str],
    sizes: dict[str, int],
) -> StagingPlan:
    """required minus present, with byte totals for the plan the agent sees."""
    seen: set[str] = set()
    missing, present = [], []
    for ref in required:
        if ref in seen:
            continue
        seen.add(ref)
        (present if ref in present_at_target else missing).append(ref)
    return StagingPlan(
        to_transfer=missing,
        present=present,
        bytes_to_move=sum(sizes.get(r, 0) for r in missing),
    )
