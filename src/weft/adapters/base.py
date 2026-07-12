"""SiteAdapter: the control-plane contract every site kind implements.

Adapters are thin: they move small control files, invoke the shim, and
submit/poll/cancel jobs. Bulk data movement belongs to TransferMethods
(doc 02 §6) — control and data planes stay separate.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..errors import WeftError


@dataclass
class ShimResult:
    rc: int
    out: str
    err: str

    def json(self) -> dict:
        try:
            return json.loads(self.out)
        except json.JSONDecodeError as e:
            raise WeftError(
                "site.unreachable",
                f"shim returned non-JSON output: {self.out[:200]!r}",
                stage="infra",
                hints={"stderr": self.err[:500]},
            ) from e


class SiteAdapter(ABC):
    """One instance per registered site; stateless between calls."""

    name: str
    kind: str

    @property
    @abstractmethod
    def root(self) -> str:
        """Absolute weft_root path on the site."""

    @abstractmethod
    def ensure_bootstrap(self) -> None:
        """Make the site usable: shim + pixi under root/bin (idempotent)."""

    @abstractmethod
    def shim(self, argv: list[str], *, timeout: float = 60.0) -> ShimResult:
        """Run weft-shim <argv...> on the site."""

    @abstractmethod
    def run_cmd(self, script: str, *, timeout: float = 120.0) -> ShimResult:
        """Run a raw shell snippet on the site (bootstrap + guarded shell)."""

    @abstractmethod
    def write_file(self, rel: str, data: bytes, mode: int = 0o644) -> None:
        """Write a small control file under root/rel."""

    @abstractmethod
    def read_file(self, rel: str, max_bytes: int | None = None) -> bytes:
        """Read a small control file under root/rel."""

    @abstractmethod
    def transfer_endpoint(self) -> dict:
        """How TransferMethods reach this site's CAS.

        {"method": "local-link", "cas_root": ...} or
        {"method": "rsync-ssh", "host": ..., "cas_root": ...}
        """

    # -- job control ------------------------------------------------------

    @abstractmethod
    def submit(self, jobdir_rel: str, task: dict) -> str:
        """Start the prepared job directory; return a scheduler-native handle."""

    @abstractmethod
    def poll_job(self, handle: str, jobdir_rel: str) -> dict:
        """{"state": running|exited|lost|missing, "exit_code": int?, ...}"""

    def poll_jobs(self, items: list[tuple[str, str]]) -> dict[str, dict]:
        """Batched poll: [(handle, jobdir_rel)] -> {handle: status}.

        One site round-trip per *interval*, not per job (doc 02 §5's
        batched-polling requirement). Base fallback loops poll_job for
        adapters without a cheaper batch primitive. A transport failure
        raises site.unreachable for the whole batch — the caller treats it
        as one site-level outage, not N job failures.
        """
        return {h: self.poll_job(h, rel) for h, rel in items}

    @abstractmethod
    def cancel(self, handle: str, jobdir_rel: str) -> None: ...

    # -- shared helpers ----------------------------------------------------

    def path(self, rel: str) -> str:
        return f"{self.root}/{rel}"

    @property
    def pixi_bin(self) -> str:
        return self.path("bin/pixi")

    def probe(self) -> dict:
        return self.shim(["probe"], timeout=30).json()

    def load(self) -> dict:
        """What is realistically available *now* (vs. probed capabilities).

        Base: host load average, free memory, logged-in users. Scheduler
        adapters extend with queue depth, idle CPUs, and wait estimates.
        """
        info = self.shim(["load"], timeout=20).json()
        cpus = max(1, int(info.get("cpus", 1)))
        info["load_fraction"] = round(float(info.get("load5", 0)) / cpus, 3)
        return info

    def run_activated(self, script: str, *, timeout: float = 120.0) -> ShimResult:
        """Run a snippet that sources env activation: conda activate.d
        hooks may contain bashisms, so prefer bash where it exists."""
        import shlex as _sh
        q = _sh.quote(script)
        return self.run_cmd(
            f"if command -v bash >/dev/null 2>&1; then bash -c {q}; "
            f"else sh -c {q}; fi", timeout=timeout)

    def file_exists(self, rel: str) -> bool:
        r = self.run_cmd(f"test -e {self.path(rel)!r} && echo yes || echo no")
        return r.out.strip() == "yes"
