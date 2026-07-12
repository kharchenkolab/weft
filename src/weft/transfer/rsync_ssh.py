"""rsync-ssh: the workhorse transfer for laptop <-> workstation/login node.

Blob layouts are identical on both ends (xx/<sha256>), so a transfer is one
rsync --files-from batch over the adapter's multiplexed ssh socket.
Destination verification is a batched `sha256sum -c` — a method that
returns has proven the bytes correct (doc 04 §4).
"""

from __future__ import annotations

import shlex
import subprocess
import tempfile
from pathlib import Path

from ..cas import LocalCAS
from ..errors import WeftError
from ..ids import hash_file


class RsyncSSH:
    name = "rsync-ssh"

    # measured per-pair throughput could refine this; a flat guess is honest
    ASSUMED_MBPS = 40.0

    def estimate(self, blobs, endpoint):
        total = sum(s for _, s in blobs)
        return {"bytes": total,
                "seconds_guess": round(total / (self.ASSUMED_MBPS * 1e6 / 8), 1)}

    @staticmethod
    def _rsh(endpoint: dict) -> str:
        return shlex.join(["ssh", *endpoint["ssh_opts"]])

    def _rsync(self, args: list[str], timeout: float = 3600) -> None:
        proc = subprocess.run(["rsync", *args], capture_output=True, text=True,
                              timeout=timeout)
        if proc.returncode != 0:
            transport = proc.returncode in (10, 11, 12, 30, 35, 255)
            raise WeftError(
                "data.transfer_failed" if not transport else "site.unreachable",
                f"rsync failed (rc={proc.returncode})",
                stage="staging", retryable=True,
                hints={"stderr": proc.stderr[-500:],
                       "resumable": "yes — rsync restarts partial blobs"},
            )

    def transfer(self, blobs, cas: LocalCAS, endpoint) -> None:
        if not blobs:
            return
        with tempfile.NamedTemporaryFile("w", suffix=".list", delete=False) as f:
            for digest, _ in blobs:
                f.write(f"{digest[:2]}/{digest}\n")
            listfile = f.name
        dest = f"{endpoint['destination']}:{endpoint['cas_root']}/"
        self._rsync([
            "-e", self._rsh(endpoint), "--partial", "--compress",
            "--chmod=Fu+rw", f"--files-from={listfile}",
            str(cas.root) + "/", dest,
        ])
        self._verify_remote(blobs, endpoint)

    def _verify_remote(self, blobs, endpoint) -> None:
        checklist = "".join(
            f"{digest}  {digest[:2]}/{digest}\n" for digest, _ in blobs
        )
        proc = subprocess.run(
            ["ssh", *endpoint["ssh_opts"], endpoint["destination"],
             f"cd {shlex.quote(endpoint['cas_root'])} && sha256sum -c --quiet"],
            input=checklist.encode(), capture_output=True, timeout=1800,
        )
        if proc.returncode != 0:
            raise WeftError(
                "data.verify_failed",
                "post-transfer hash verification failed at destination",
                stage="staging", retryable=True,
                hints={"stderr": proc.stderr.decode()[-500:]},
            )

    def fetch(self, blobs, cas: LocalCAS, endpoint) -> None:
        if not blobs:
            return
        with tempfile.NamedTemporaryFile("w", suffix=".list", delete=False) as f:
            for digest, _ in blobs:
                f.write(f"{digest[:2]}/{digest}\n")
            listfile = f.name
        src = f"{endpoint['destination']}:{endpoint['cas_root']}/"
        self._rsync([
            "-e", self._rsh(endpoint), "--partial", "--compress",
            f"--files-from={listfile}", src, str(cas.root) + "/",
        ])
        for digest, _ in blobs:
            p = Path(cas.root) / digest[:2] / digest
            if not p.exists() or hash_file(p).sha256 != digest:
                raise WeftError(
                    "data.verify_failed",
                    f"fetched blob {digest[:12]} failed verification",
                    stage="staging", retryable=True,
                )
