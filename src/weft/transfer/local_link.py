"""local-link: hardlink/copy between CAS roots on the same machine."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from ..cas import LocalCAS
from ..errors import WeftError
from ..ids import hash_file


class LocalLink:
    name = "local-link"

    def estimate(self, blobs, endpoint):
        return {"bytes": sum(s for _, s in blobs), "seconds_guess": 0.0}

    @staticmethod
    def _dst(endpoint: dict, digest: str) -> Path:
        return Path(endpoint["cas_root"]) / digest[:2] / digest

    def transfer(self, blobs, cas: LocalCAS, endpoint, progress=None,
                 verify=None) -> None:
        done_bytes = done_files = 0
        for digest, size in blobs:
            done_bytes += size
            done_files += 1
            if progress:
                progress({"bytes_done": done_bytes, "files_done": done_files})
            src = cas.open_blob(f"dref:{digest}")
            dst = self._dst(endpoint, digest)
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(".tmp")
            try:
                os.link(src, tmp)
            except OSError:
                shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            # hardlinks share the inode we just hashed from — only copies
            # need re-verification, and only paranoidly
            if dst.stat().st_ino != src.stat().st_ino:
                if hash_file(dst).sha256 != digest:
                    dst.unlink()
                    raise WeftError(
                        "data.verify_failed",
                        f"blob {digest[:12]} corrupted in local transfer",
                        stage="staging", retryable=True,
                    )

    def fetch(self, blobs, cas: LocalCAS, endpoint, progress=None) -> None:
        for digest, _ in blobs:
            src = self._dst(endpoint, digest)
            if not src.exists():
                raise WeftError(
                    "data.missing", f"blob {digest[:12]} not at endpoint",
                    stage="staging",
                )
            if cas.kind_of(f"dref:{digest}") is None:
                if hash_file(src).sha256 != digest:
                    raise WeftError(
                        "data.verify_failed",
                        f"blob {digest[:12]} corrupt at endpoint", stage="staging",
                    )
                cas.put_bytes(src.read_bytes()) if src.stat().st_size < (1 << 20) \
                    else self._link_into(cas, src, digest)

    @staticmethod
    def _link_into(cas: LocalCAS, src: Path, digest: str) -> None:
        dst = cas.root / digest[:2] / digest
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        try:
            os.link(src, tmp)
        except OSError:
            shutil.copy2(src, tmp)
        os.replace(tmp, dst)
