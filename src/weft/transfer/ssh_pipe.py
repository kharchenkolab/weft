"""ssh-pipe: tar over the ssh control channel — the transfer of last
resort for sites without rsync. Slower (no delta, no resume), but needs
nothing on the far side beyond sh + tar + sha256sum, which the shim
already requires. Verification is the same batched `sha256sum -c`.
"""

from __future__ import annotations

import io
import shlex
import subprocess
import tarfile
import time

from ..cas import LocalCAS
from ..errors import WeftError
from ..ids import hash_file


class _CountingWriter:
    """File-like wrapper that counts bytes into a pipe (for progress)."""

    def __init__(self, fh, progress, min_interval: float = 0.5):
        self._fh = fh
        self._progress = progress
        self._min_interval = min_interval
        self._done = 0
        self._last = 0.0

    def write(self, data: bytes) -> int:
        self._fh.write(data)
        self._done += len(data)
        now = time.time()
        if self._progress and now - self._last >= self._min_interval:
            self._last = now
            self._progress({"bytes_done": self._done})
        return len(data)

    def flush(self):
        self._fh.flush()

    def close(self):
        pass  # the caller owns the pipe


class SshPipe:
    name = "ssh-pipe"

    ASSUMED_MBPS = 20.0

    def estimate(self, blobs, endpoint):
        total = sum(s for _, s in blobs)
        return {"bytes": total,
                "seconds_guess": round(total / (self.ASSUMED_MBPS * 1e6 / 8), 1)}

    @staticmethod
    def _ssh(endpoint: dict, remote_cmd: str) -> list[str]:
        return ["ssh", *endpoint["ssh_opts"], endpoint["destination"], remote_cmd]

    def transfer(self, blobs, cas: LocalCAS, endpoint, progress=None,
                 verify=None) -> None:
        if not blobs:
            return
        root = shlex.quote(endpoint["cas_root"])
        proc = subprocess.Popen(
            self._ssh(endpoint, f"mkdir -p {root} && cd {root} && tar -xf -"),
            stdin=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        # stream the archive — never materialized in memory — counting
        # bytes into the pipe for progress
        counter = _CountingWriter(proc.stdin, progress)
        try:
            with tarfile.open(fileobj=counter, mode="w|") as tar:
                for digest, _ in blobs:
                    tar.add(cas.open_blob(f"dref:{digest}"),
                            arcname=f"{digest[:2]}/{digest}")
            proc.stdin.close()
        except BrokenPipeError:
            pass
        rc = proc.wait(timeout=3600)
        if rc != 0:
            raise WeftError(
                "data.transfer_failed",
                f"ssh-pipe transfer failed (rc={rc})",
                stage="staging", retryable=True,
                hints={"stderr": proc.stderr.read().decode()[-500:],
                       "resumable": "no — ssh-pipe restarts whole batches"},
            )
        verify = verify or {}
        checklist = "".join(
            f"{verify.get(d, d)}  {d[:2]}/{d}\n" for d, _ in blobs
            if verify.get(d, d) is not None
        )
        if not checklist:
            return
        v = subprocess.run(
            self._ssh(endpoint, f"cd {root} && sha256sum -c >/dev/null"),
            input=checklist.encode(), capture_output=True, timeout=1800,
        )
        if v.returncode != 0:
            raise WeftError(
                "data.verify_failed",
                "post-transfer verification failed at destination",
                stage="staging", retryable=True,
                hints={"stderr": v.stderr.decode()[-500:]},
            )

    def fetch(self, blobs, cas: LocalCAS, endpoint, progress=None) -> None:
        if not blobs:
            return
        root = shlex.quote(endpoint["cas_root"])
        names = " ".join(shlex.quote(f"{d[:2]}/{d}") for d, _ in blobs)
        proc = subprocess.run(
            self._ssh(endpoint, f"cd {root} && tar -cf - {names}"),
            capture_output=True, timeout=3600,
        )
        if proc.returncode != 0:
            raise WeftError(
                "data.transfer_failed", "ssh-pipe fetch failed",
                stage="staging", retryable=True,
                hints={"stderr": proc.stderr.decode()[-500:]},
            )
        with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
            for digest, _ in blobs:
                member = tar.extractfile(f"{digest[:2]}/{digest}")
                if member is None:
                    raise WeftError("data.missing",
                                    f"blob {digest[:12]} absent at endpoint",
                                    stage="staging")
                cas.put_bytes(member.read())
        for digest, _ in blobs:
            p = cas.open_blob(f"dref:{digest}")
            if hash_file(p).sha256 != digest:
                raise WeftError(
                    "data.verify_failed",
                    f"fetched blob {digest[:12]} failed verification",
                    stage="staging", retryable=True,
                )
