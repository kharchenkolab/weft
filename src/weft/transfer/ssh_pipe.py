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

from ..cas import LocalCAS
from ..errors import WeftError
from ..ids import hash_file


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

    def transfer(self, blobs, cas: LocalCAS, endpoint) -> None:
        if not blobs:
            return
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for digest, _ in blobs:
                tar.add(cas.open_blob(f"dref:{digest}"),
                        arcname=f"{digest[:2]}/{digest}")
        root = shlex.quote(endpoint["cas_root"])
        proc = subprocess.run(
            self._ssh(endpoint, f"mkdir -p {root} && cd {root} && tar -xf -"),
            input=buf.getvalue(), capture_output=True, timeout=3600,
        )
        if proc.returncode != 0:
            raise WeftError(
                "data.transfer_failed",
                f"ssh-pipe transfer failed (rc={proc.returncode})",
                stage="staging", retryable=True,
                hints={"stderr": proc.stderr.decode()[-500:],
                       "resumable": "no — ssh-pipe restarts whole batches"},
            )
        checklist = "".join(f"{d}  {d[:2]}/{d}\n" for d, _ in blobs)
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

    def fetch(self, blobs, cas: LocalCAS, endpoint) -> None:
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
