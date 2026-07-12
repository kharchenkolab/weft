"""Content addressing primitives: canonical JSON, file/tree/chunk hashing.

Every identity in weft (EnvID, DataRef, TaskID, spec hash) reduces to
sha256 over a canonical byte serialization defined here.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

# Files at or above this size are chunk-hashed (Merkle) so transfers can
# resume and append-only growth can delta-transfer later.
CHUNK_SIZE = 64 * 1024 * 1024

ENVID_SCHEME = "env:v1:"
DREF_SCHEME = "dref:"
TASK_SCHEME = "task:v1:"
SPEC_SCHEME = "spec:v1:"


def canonical_json(obj) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class FileDigest:
    sha256: str          # whole-file or merkle root — this is the DataRef hash
    size: int
    chunks: list[str] | None  # chunk hashes when chunked, else None
    # for chunked files the CAS name is the merkle root, which remote
    # `sha256sum -c` can never reproduce — so we also carry the plain
    # content hash, computed in the same read pass, for wire verification
    plain_sha256: str | None = None


def hash_file(path: Path) -> FileDigest:
    size = path.stat().st_size
    if size < CHUNK_SIZE:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return FileDigest(h.hexdigest(), size, None)
    chunks: list[str] = []
    plain = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            plain.update(chunk)
            chunks.append(hashlib.sha256(chunk).hexdigest())
    root = sha256_bytes(canonical_json({"merkle": chunks, "size": size}))
    return FileDigest(root, size, chunks, plain.hexdigest())


def _is_exec(mode: int) -> bool:
    return bool(mode & stat.S_IXUSR)


def tree_manifest(root: Path) -> list[dict]:
    """Canonical manifest of a directory tree: sorted (path, mode, size, hash).

    Mode is reduced to an executable bit — full permission bits are not
    portable across sites and would make tree hashes machine-dependent.
    Symlinks are recorded by target, not followed.
    """
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            rel = str(p.relative_to(root))
            if p.is_symlink():
                entries.append({"path": rel, "kind": "link", "target": os.readlink(p)})
                continue
            st = p.stat()
            d = hash_file(p)
            entry = {
                "path": rel,
                "kind": "file",
                "exec": _is_exec(st.st_mode),
                "size": d.size,
                "sha256": d.sha256,
            }
            if d.plain_sha256:
                entry["sha256_plain"] = d.plain_sha256
            entries.append(entry)
    return entries


def hash_tree(root: Path) -> tuple[str, list[dict]]:
    manifest = tree_manifest(root)
    return sha256_bytes(canonical_json(manifest)), manifest


def env_id(canonical_lock: dict) -> str:
    return ENVID_SCHEME + sha256_bytes(canonical_json(canonical_lock))


def data_ref(hex_digest: str) -> str:
    return DREF_SCHEME + hex_digest


def spec_id(canonical_spec: dict) -> str:
    return SPEC_SCHEME + sha256_bytes(canonical_json(canonical_spec))


def task_id(payload: dict) -> str:
    return TASK_SCHEME + sha256_bytes(canonical_json(payload))
