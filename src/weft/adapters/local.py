"""Local adapter: direct subprocess execution, reference oracle for all
other adapters (doc 02 §5). Uses the same shim and detached-run semantics
as remote sites so lifecycle behavior — including crash reconciliation —
is identical everywhere.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from ..errors import WeftError
from .base import ShimResult, SiteAdapter

SHIM_SRC = Path(__file__).resolve().parent.parent / "shim" / "weft-shim"


class LocalAdapter(SiteAdapter):
    kind = "local"

    def __init__(self, name: str, root: Path, pixi_source: str | None = None):
        self.name = name
        self._root = Path(root)
        self._pixi_source = pixi_source  # local pixi binary to link into root/bin

    @property
    def root(self) -> str:
        return str(self._root)

    def _env(self) -> dict:
        env = dict(os.environ)
        env["WEFT_ROOT"] = self.root
        env["PIXI_CACHE_DIR"] = self.path("cache/pixi")
        env["PIXI_HOME"] = self.path("pixi-home")
        return env

    def ensure_bootstrap(self) -> None:
        bin_dir = self._root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        for sub in ("envs", "cas", "jobs", "tmp", "cache"):
            (self._root / sub).mkdir(exist_ok=True)
        shim_dst = bin_dir / "weft-shim"
        if not shim_dst.exists() or shim_dst.read_bytes() != SHIM_SRC.read_bytes():
            shim_dst.write_bytes(SHIM_SRC.read_bytes())
            shim_dst.chmod(0o755)
        if self._pixi_source and not (bin_dir / "pixi").exists():
            try:
                os.link(self._pixi_source, bin_dir / "pixi")
            except OSError:
                import shutil
                shutil.copy2(self._pixi_source, bin_dir / "pixi")
                (bin_dir / "pixi").chmod(0o755)
        (self._root / ".weft-site").write_text('{"bootstrap_version": 1}\n')

    def shim(self, argv: list[str], *, timeout: float = 60.0) -> ShimResult:
        proc = subprocess.run(
            [str(self._root / "bin" / "weft-shim"), *argv],
            capture_output=True, text=True, timeout=timeout, env=self._env(),
        )
        return ShimResult(proc.returncode, proc.stdout, proc.stderr)

    def run_cmd(self, script: str, *, timeout: float = 120.0) -> ShimResult:
        proc = subprocess.run(
            ["sh", "-c", script],
            capture_output=True, text=True, timeout=timeout, env=self._env(),
        )
        return ShimResult(proc.returncode, proc.stdout, proc.stderr)

    def write_file(self, rel: str, data: bytes, mode: int = 0o644) -> None:
        p = self._root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        p.chmod(mode)

    def read_file(self, rel: str, max_bytes: int | None = None) -> bytes:
        p = self._root / rel
        if not p.exists():
            raise WeftError("data.missing", f"no such file on site: {rel}", stage="infra")
        data = p.read_bytes()
        return data[:max_bytes] if max_bytes else data

    def transfer_endpoint(self) -> dict:
        return {"method": "local-link", "cas_root": self.path("cas")}

    # -- job control ------------------------------------------------------

    def submit(self, jobdir_rel: str, task: dict) -> str:
        r = self.shim(["run", "--dir", self.path(jobdir_rel)])
        if r.rc != 0:
            raise WeftError(
                "job.nonzero_exit", f"shim run failed: {r.err[:300]}", stage="submit"
            )
        return f"pid:{r.json().get('pid', 0)}"

    def poll_job(self, handle: str, jobdir_rel: str) -> dict:
        return self.shim(["status", "--dir", self.path(jobdir_rel)]).json()

    def cancel(self, handle: str, jobdir_rel: str) -> None:
        if handle.startswith("pid:"):
            pid = handle[4:]
            # negative pid: kill the whole detached process group
            self.run_cmd(f"kill -TERM -{shlex.quote(pid)} 2>/dev/null; true")
