"""Cloud adapter: ephemeral SSH sites behind a provisioner seam (doc 02 §5).

The provisioner models the SkyPilot boundary: launch/describe/teardown plus
a cost rate. Everything after `launch` is the ordinary SSH control plane —
a provisioned instance is just an SSH site whose lifetime weft owns.

Money is treated as a *hard* constraint (doc 05 §6):
  * pre-launch: estimated spend (rate × max_hours horizon) must fit the
    budget or the launch is refused with `budget.exceeded` — nothing is
    provisioned;
  * while running: every control-plane touch re-checks accrued spend; on
    breach the watchdog cancels, tears the instance down, and only then
    raises — a runaway can cost at most one poll interval of overrun.
"""

from __future__ import annotations

import threading
import time
from typing import Protocol

from ..errors import WeftError
from .base import ShimResult, SiteAdapter
from .ssh import SSHAdapter


class CloudProvisioner(Protocol):
    def launch(self) -> str: ...
    def describe(self, handle: str) -> dict:
        """{"host", "port", "user", "ssh_opts", "root"}"""
        ...
    def teardown(self, handle: str) -> None: ...
    def rate_usd_per_hour(self) -> float: ...


class CloudAdapter(SiteAdapter):
    kind = "cloud"

    def __init__(
        self, name: str, provisioner: CloudProvisioner, *,
        budget: dict | None = None,
        synthetic_caps: dict | None = None,
        pixi_source: str | None = None,
        pixi_unpack_source: str | None = None,
        emit=None,
    ):
        self.name = name
        self.provisioner = provisioner
        self.budget = budget or {}
        self.synthetic_caps = synthetic_caps or {}
        self.pixi_source = pixi_source
        self.pixi_unpack_source = pixi_unpack_source
        self._emit = emit or (lambda *a, **k: None)
        self._lock = threading.RLock()
        self._handle: str | None = None
        self._inner: SSHAdapter | None = None
        self._launched_at: float | None = None

    # -- lifecycle -------------------------------------------------------

    def accrued_usd(self) -> float:
        if self._launched_at is None:
            return 0.0
        hours = (time.time() - self._launched_at) / 3600
        return hours * self.provisioner.rate_usd_per_hour()

    def _precheck_budget(self) -> None:
        max_usd = self.budget.get("max_usd")
        if max_usd is None:
            return
        rate = self.provisioner.rate_usd_per_hour()
        horizon_h = float(self.budget.get("max_hours", 1.0))
        estimate = rate * horizon_h
        if estimate > max_usd:
            raise WeftError(
                "budget.exceeded",
                f"launch refused: {horizon_h:.2f}h at ${rate:.2f}/h ≈ "
                f"${estimate:.2f} exceeds the ${max_usd:.2f} cap",
                stage="submit",
                hints={"rate_usd_per_hour": rate, "cap_usd": max_usd,
                       "source": "site budget set by the user",
                       "suggestion": "raise the cap, shorten max_hours, or "
                                     "pick a cheaper instance"},
            )

    def _watchdog(self) -> None:
        max_usd = self.budget.get("max_usd")
        if max_usd is None or self._launched_at is None:
            return
        spent = self.accrued_usd()
        if spent > max_usd:
            self._emit("budget.watchdog", site=self.name,
                       spent_usd=round(spent, 4), cap_usd=max_usd)
            self.teardown()
            raise WeftError(
                "budget.exceeded",
                f"runaway watchdog: accrued ${spent:.2f} exceeds the "
                f"${max_usd:.2f} cap — instance torn down",
                stage="running",
                hints={"spent_usd": round(spent, 4), "cap_usd": max_usd,
                       "instance": "terminated",
                       "suggestion": "results of unfinished jobs are lost; "
                                     "raise the cap before retrying"},
            )

    def _ready(self) -> SSHAdapter:
        with self._lock:
            if self._inner is None:
                self._precheck_budget()
                self._handle = self.provisioner.launch()
                self._launched_at = time.time()
                self._emit("cloud.launched", site=self.name, handle=self._handle,
                           rate_usd_per_hour=self.provisioner.rate_usd_per_hour())
                info = self.provisioner.describe(self._handle)
                self._inner = SSHAdapter(
                    self.name, info["host"], info["root"],
                    user=info.get("user"), port=info.get("port"),
                    ssh_opts=info.get("ssh_opts"),
                    pixi_source=self.pixi_source,
                    pixi_unpack_source=self.pixi_unpack_source,
                )
                self._inner.ensure_bootstrap()
            self._watchdog()
            return self._inner

    def teardown(self) -> None:
        with self._lock:
            if self._handle is not None:
                try:
                    self.provisioner.teardown(self._handle)
                finally:
                    self._emit("cloud.teardown", site=self.name,
                               handle=self._handle,
                               spent_usd=round(self.accrued_usd(), 4))
                    self._handle = None
                    self._inner = None
                    self._launched_at = None

    @property
    def launched(self) -> bool:
        return self._inner is not None

    # -- SiteAdapter delegation ----------------------------------------------

    @property
    def root(self) -> str:
        return self._inner.root if self._inner else "/tmp/.weft-cloud-pending"

    def ensure_bootstrap(self) -> None:
        pass  # deferred: nothing is provisioned until first use

    def probe(self) -> dict:
        # capability record synthesized from the resource ask (doc 02 §5);
        # probing for real would cost a launch
        return {
            "shim_version": None, "os": "linux", "arch": "x86_64",
            "internet": True,
            "runtimes": {"docker": False, "apptainer": "", "rsync": True},
            "scheduler": {"type": "none"}, "module_system": False,
            "gpus": self.synthetic_caps.get("gpus", []),
            "cuda_driver": self.synthetic_caps.get("cuda_driver", ""),
            "cpus": self.synthetic_caps.get("cpus", 8),
            "mem_gb": self.synthetic_caps.get("mem_gb", 32),
            "storage": {"weft_root": "(instance disk)", "free_gb": 100},
            **{k: v for k, v in self.synthetic_caps.items()
               if k not in ("gpus", "cuda_driver", "cpus", "mem_gb")},
        }

    def shim(self, argv, *, timeout: float = 60.0) -> ShimResult:
        return self._ready().shim(argv, timeout=timeout)

    def run_cmd(self, script: str, *, timeout: float = 120.0) -> ShimResult:
        return self._ready().run_cmd(script, timeout=timeout)

    def write_file(self, rel: str, data: bytes, mode: int = 0o644) -> None:
        self._ready().write_file(rel, data, mode)

    def read_file(self, rel: str, max_bytes: int | None = None) -> bytes:
        return self._ready().read_file(rel, max_bytes)

    def transfer_endpoint(self) -> dict:
        return self._ready().transfer_endpoint()

    def submit(self, jobdir_rel: str, task: dict) -> str:
        return self._ready().submit(jobdir_rel, task)

    def poll_job(self, handle: str, jobdir_rel: str) -> dict:
        return self._ready().poll_job(handle, jobdir_rel)

    def cancel(self, handle: str, jobdir_rel: str) -> None:
        if self._inner is not None:
            self._inner.cancel(handle, jobdir_rel)
