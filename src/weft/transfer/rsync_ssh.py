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

import re
import time

from ..cas import LocalCAS
from ..errors import WeftError
from ..ids import hash_file

_PROGRESS_RE = re.compile(r"^\s*([\d,]+)\s+(\d+)%")


import functools


@functools.lru_cache(maxsize=1)
def _progress_flag() -> str:
    # macOS ships openrsync, which lacks --info=progress2; --progress is
    # the portable fallback (per-file rather than whole-transfer numbers —
    # the same regex parses both)
    r = subprocess.run(["rsync", "--info=progress2", "--version"],
                       capture_output=True)
    return "--info=progress2" if r.returncode == 0 else "--progress"


class RsyncSSH:
    name = "rsync-ssh"

    # measured per-pair throughput could refine this; a flat guess is honest
    ASSUMED_MBPS = 40.0
    extra_args: list[str] = []   # e.g. ["--bwlimit=4000"] (tests, politeness)

    def estimate(self, blobs, endpoint):
        total = sum(s for _, s in blobs)
        return {"bytes": total,
                "seconds_guess": round(total / (self.ASSUMED_MBPS * 1e6 / 8), 1)}

    @staticmethod
    def _rsh(endpoint: dict) -> str:
        return shlex.join(["ssh", *endpoint["ssh_opts"]])

    def _rsync(self, args: list[str], progress=None, timeout: float = 3600) -> None:
        # progress repaints are \r-terminated, so no pipe buffering mode
        # flushes them — rsync only streams progress to a terminal. Give it
        # one.
        import os
        import pty
        master, slave = pty.openpty()
        proc = subprocess.Popen(
            ["rsync", _progress_flag(), *self.extra_args, *args],
            stdout=slave, stderr=subprocess.PIPE,
        )
        os.close(slave)
        buf, last = "", 0.0
        while True:
            try:
                chunk = os.read(master, 1024)
            except OSError:   # EIO when the child side closes
                break
            if not chunk:
                break
            buf += chunk.decode("utf-8", "replace")
            *lines, buf = re.split(r"[\r\n]", buf)
            for line in lines:
                m = _PROGRESS_RE.match(line)
                if m and progress and time.time() - last >= 0.5:
                    last = time.time()
                    progress({"bytes_done": int(m.group(1).replace(",", "")),
                              "percent": int(m.group(2))})
        os.close(master)
        rc = proc.wait(timeout=timeout)
        if rc != 0:
            transport = rc in (10, 11, 12, 30, 35, 255)
            raise WeftError(
                "data.transfer_failed" if not transport else "site.unreachable",
                f"rsync failed (rc={rc})",
                stage="staging", retryable=True,
                hints={"stderr": proc.stderr.read().decode()[-500:],
                       "resumable": "yes — rsync restarts partial blobs"},
            )

    def transfer(self, blobs, cas: LocalCAS, endpoint, progress=None,
                 verify=None) -> None:
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
        ], progress=progress)
        self._verify_remote(blobs, endpoint, verify)

    def _verify_remote(self, blobs, endpoint, verify=None) -> None:
        # chunked blobs are named by merkle root; verify their *content*
        # hash instead. None = unverifiable remotely (legacy) — rsync's own
        # transport checksums are the guarantee then.
        verify = verify or {}
        checklist = "".join(
            f"{verify.get(digest, digest)}  {digest[:2]}/{digest}\n"
            for digest, _ in blobs
            if verify.get(digest, digest) is not None
        )
        if not checklist:
            return
        proc = subprocess.run(
            ["ssh", *endpoint["ssh_opts"], endpoint["destination"],
             f"cd {shlex.quote(endpoint['cas_root'])} && sha256sum -c - >/dev/null"],
            input=checklist.encode(), capture_output=True, timeout=1800,
        )
        if proc.returncode != 0:
            raise WeftError(
                "data.verify_failed",
                "post-transfer hash verification failed at destination",
                stage="staging", retryable=True,
                hints={"stderr": proc.stderr.decode()[-500:]},
            )

    def fetch(self, blobs, cas: LocalCAS, endpoint, progress=None) -> None:
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
        ], progress=progress)
        for digest, _ in blobs:
            p = Path(cas.root) / digest[:2] / digest
            if not p.exists() or hash_file(p).sha256 != digest:
                raise WeftError(
                    "data.verify_failed",
                    f"fetched blob {digest[:12]} failed verification",
                    stage="staging", retryable=True,
                )
