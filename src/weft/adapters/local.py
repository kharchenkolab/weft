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

    def __init__(self, name: str, root: Path, pixi_source: str | None = None,
                 shared: bool = False):
        self.name = name
        self._root = Path(root)
        self._pixi_source = pixi_source  # local pixi binary to link into root/bin
        self.shared = shared
        # shared roots: subprocesses create group-usable files
        self._preexec = (lambda: os.umask(0o002)) if shared else None

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
        if self._pixi_source:
            # pixi plus its siblings (pixi-unpack for packed realizations)
            src_dir = Path(self._pixi_source).parent
            for name, src in [("pixi", Path(self._pixi_source)),
                              ("pixi-unpack", src_dir / "pixi-unpack")]:
                dst = bin_dir / name
                if dst.exists() or not src.exists():
                    continue
                try:
                    os.link(src, dst)
                except OSError:
                    import shutil
                    shutil.copy2(src, dst)
                dst.chmod(0o755)
        (self._root / ".weft-site").write_text('{"bootstrap_version": 1}\n')

    def shim(self, argv: list[str], *, timeout: float = 60.0) -> ShimResult:
        proc = subprocess.run(
            [str(self._root / "bin" / "weft-shim"), *argv],
            capture_output=True, text=True, timeout=timeout, env=self._env(),
            preexec_fn=self._preexec,
        )
        return ShimResult(proc.returncode, proc.stdout, proc.stderr)

    def run_cmd(self, script: str, *, timeout: float = 120.0) -> ShimResult:
        proc = subprocess.run(
            ["sh", "-c", script],
            capture_output=True, text=True, timeout=timeout, env=self._env(),
            preexec_fn=self._preexec,
        )
        return ShimResult(proc.returncode, proc.stdout, proc.stderr)

    def write_file(self, rel: str, data: bytes, mode: int = 0o644) -> None:
        p = self._root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if self.shared:
            mode |= 0o020  # group-writable on shared roots
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

    def poll_jobs(self, items: list[tuple[str, str]]) -> dict[str, dict]:
        if not items:
            return {}
        import json
        by_dir = {self.path(rel): h for h, rel in items}
        proc = subprocess.run(
            [str(self._root / "bin" / "weft-shim"), "status-batch"],
            input="\n".join(by_dir) + "\n",
            capture_output=True, text=True, timeout=60 + 0.05 * len(items),
            env=self._env(),
        )
        out: dict[str, dict] = {}
        for line in proc.stdout.splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            handle = by_dir.get(rec.pop("dir", ""))
            if handle is not None:
                out[handle] = rec
        for h, _ in items:
            out.setdefault(h, {"state": "unknown"})
        return out

    def cancel(self, handle: str, jobdir_rel: str) -> None:
        if handle.startswith("pid:"):
            pid = handle[4:]
            # negative pid: kill the whole detached process group
            self.run_cmd(f"kill -s TERM -- -{shlex.quote(pid)} 2>/dev/null; true")
