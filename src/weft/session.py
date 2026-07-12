"""Session environments: mutable, unhashed, single-site scratch (doc 03 §7).

The interactive loop — "try importing X… now add Y" — gets a scratch clone
of a realized environment that the agent may mutate incrementally via
`pixi add` (seconds against a warm package cache). The rules that protect
the identity model:

  * session envs are unhashed and single-site; nothing that runs in one can
    enter the project record (session_exec returns output, never manifests)
  * `snapshot` synthesizes the minimal spec delta over the base, re-solves
    properly, and returns a real EnvID — the citable re-run is then cheap
    because every package is already in the site cache.

Sessions need index access from the site; on air-gapped sites the error
says so instead of half-working.
"""

from __future__ import annotations

import shlex
import uuid

from .adapters.base import SiteAdapter
from .envman import EnvManager
from .errors import WeftError
from .realize import env_dir_rel
from .store import Store


class SessionManager:
    def __init__(self, store: Store, envman: EnvManager, runner=None):
        self.store = store
        self.envman = envman
        self.runner = runner   # for auto-realizing a base env (ergonomics)

    def start(self, base: str | dict, adapter: SiteAdapter) -> dict:
        """Accepts an EnvID *or a spec* — exploration should not cost three
        round trips (ensure → throwaway task to realize → start)."""
        if isinstance(base, dict):
            base_env_id = self.envman.ensure(base)["env_id"]
        else:
            base_env_id = base
        env_row = self.store.get_env(base_env_id)
        if not env_row:
            raise WeftError("task.invalid", f"unknown EnvID: {base_env_id}", stage="solve")
        site_row = self.store.get_site(adapter.name)
        caps = (site_row or {}).get("capabilities") or {}
        from .capability import compute_view
        if not compute_view(caps).get("internet", False):
            raise WeftError(
                "env.unsatisfiable_on_site",
                f"session envs need package-index access from {adapter.name}",
                stage="realize",
                hints={"suggestion": "extend the spec instead and let weft "
                                     "deliver a packed realization"},
            )
        base_rel = env_dir_rel(base_env_id)
        if not adapter.file_exists(f"{base_rel}/.weft-ready"):
            # realizing the base is weft's errand, not the agent's
            if self.runner is None:
                raise WeftError(
                    "env.not_realized",
                    f"env {base_env_id} is not realized on {adapter.name}",
                    stage="realize",
                    hints={"suggestion": "run any task with it there first"})
            from .realize import ensure_realization
            ensure_realization(
                base_env_id, env_row, adapter, self.store,
                caps=(site_row or {}).get("capabilities"),
                site_config=(site_row or {}).get("config"),
                pack_tools={"pixi_pack": self.runner.pixi_pack,
                            "cas": self.runner.cas,
                            "transfers": self.runner.transfers,
                            "solvers": self.envman.solvers})
        session_id = "ses_" + uuid.uuid4().hex[:10]
        rel = f"sessions/{session_id}"
        # clone the *project* (manifest + lock); the install is a fresh
        # hardlink forest from the shared package cache — cheap, and the
        # base realization stays immutable
        r = adapter.run_cmd(
            f"mkdir -p {shlex.quote(adapter.path(rel))} && "
            f"cp {shlex.quote(adapter.path(base_rel))}/pixi.toml "
            f"{shlex.quote(adapter.path(base_rel))}/pixi.lock "
            f"{shlex.quote(adapter.path(rel))}/ && "
            f"{shlex.quote(adapter.pixi_bin)} install "
            f"--manifest-path {shlex.quote(adapter.path(rel))}/pixi.toml",
            timeout=900,
        )
        if r.rc != 0:
            raise WeftError(
                "env.realize_failed", "session clone failed", stage="realize",
                hints={"log_tail": (r.err or r.out)[-1000:]},
            )
        self.store.put_session(session_id, base_env_id, adapter.name, rel)
        self.store.emit("session.started", session=session_id,
                        base=base_env_id, site=adapter.name)
        return {
            "session_id": session_id, "site": adapter.name,
            "base_env_id": base_env_id,
            "warning": "unhashed scratch environment — snapshot before "
                       "recording any result",
        }

    def _get(self, session_id: str) -> dict:
        s = self.store.get_session(session_id)
        if not s or s["state"] != "active":
            raise WeftError(
                "task.invalid", f"no active session {session_id}", stage="infra",
            )
        return s

    def exec(self, session_id: str, adapter: SiteAdapter, cmd: str) -> dict:
        s = self._get(session_id)
        manifest = adapter.path(f"{s['location']}/pixi.toml")
        r = adapter.run_cmd(
            f"cd {shlex.quote(adapter.path(s['location']))} && "
            f"eval \"$({shlex.quote(adapter.pixi_bin)} shell-hook "
            f"--manifest-path {shlex.quote(manifest)})\" && ( {cmd} )",
            timeout=600,
        )
        self.store.audit_log("agent", "session.exec", site=adapter.name,
                             command=cmd, why=f"session {session_id}",
                             result=f"rc={r.rc}")
        return {"rc": r.rc, "stdout": r.out[-8000:], "stderr": r.err[-4000:]}

    def install(self, session_id: str, adapter: SiteAdapter,
                conda: list[str] | None = None, pypi: list[str] | None = None) -> dict:
        s = self._get(session_id)
        conda, pypi = list(conda or []), list(pypi or [])
        if not conda and not pypi:
            raise WeftError("task.invalid", "nothing to install", stage="realize")
        manifest = adapter.path(f"{s['location']}/pixi.toml")
        parts = []
        if conda:
            parts.append(
                f"{shlex.quote(adapter.pixi_bin)} add --manifest-path "
                f"{shlex.quote(manifest)} {' '.join(shlex.quote(_pixi_spec(c)) for c in conda)}"
            )
        if pypi:
            parts.append(
                f"{shlex.quote(adapter.pixi_bin)} add --pypi --manifest-path "
                f"{shlex.quote(manifest)} {' '.join(shlex.quote(_pixi_spec(p)) for p in pypi)}"
            )
        r = adapter.run_cmd(" && ".join(parts), timeout=900)
        if r.rc != 0:
            raise WeftError(
                "env.solve_conflict",
                "incremental install failed in session",
                stage="realize",
                hints={"log_tail": (r.err or r.out)[-1500:],
                       "requested": conda + pypi},
            )
        self.store.session_add_deps(session_id, conda, pypi)
        self.store.emit("session.installed", session=session_id,
                        conda=conda, pypi=pypi)
        return {"installed": {"conda": conda, "pypi": pypi},
                "session_id": session_id}

    def run_installer(self, session_id: str, adapter: SiteAdapter, cmd: str,
                      note: str = "") -> dict:
        """The bespoke install that no index can express — an R
        install.packages, a pip install -e, a vendored make install. A
        normal, supported move: it runs in the session AND is captured, so
        `snapshot` can carry it into the spec as a labeled post_install
        step instead of losing it."""
        s = self._get(session_id)
        manifest = adapter.path(f"{s['location']}/pixi.toml")
        r = adapter.run_activated(
            f"cd {shlex.quote(adapter.path(s['location']))} && "
            f"eval \"$({shlex.quote(adapter.pixi_bin)} shell-hook "
            f"--manifest-path {shlex.quote(manifest)})\" && ( {cmd} )",
            timeout=3600)
        if r.rc != 0:
            raise WeftError(
                "env.realize_failed",
                "session installer failed",
                stage="realize",
                hints={"command": cmd, "log_tail": (r.err or r.out)[-1500:]})
        self.store.session_add_installer(session_id, cmd, note)
        self.store.emit("session.installer", session=session_id, cmd=cmd[:200])
        return {"session_id": session_id, "installed": cmd,
                "captured": True,
                "note": "snapshot will carry this as a post_install step "
                        "(grade: escape-hatch) with your note attached"}

    def snapshot(self, session_id: str, name: str | None = None,
                 notes: list[str] | None = None) -> dict:
        """Synthesize the spec delta, re-solve properly, return a real EnvID.
        Captured bespoke installers ride along as labeled post_install steps."""
        s = self._get(session_id)
        env_row = self.store.get_env(s["base_env_id"])
        installers = s.get("installers") or []
        spec = {
            "name": name or f"snapshot-of-{session_id}",
            "extends": env_row["spec_hash"],
            "deps": {"conda": s["added_conda"], "pypi": s["added_pypi"]},
        }
        if installers:
            spec["post_install"] = [i["cmd"] for i in installers]
            spec["step_notes"] = {str(i): inst["note"]
                                  for i, inst in enumerate(installers)
                                  if inst.get("note")}
        if notes:
            spec["notes"] = list(notes)
        result = self.envman.ensure(spec)
        self.store.emit("session.snapshot", session=session_id,
                        env_id=result["env_id"])
        out = {**result, "spec": spec,
               "note": "re-run the final computation under this EnvID to "
                       "enter it into provenance"}
        if installers:
            out["carried_installers"] = len(installers)
        return out

    def stop(self, session_id: str, adapter: SiteAdapter) -> dict:
        s = self._get(session_id)
        adapter.run_cmd(f"rm -rf {shlex.quote(adapter.path(s['location']))}")
        self.store.set_session_state(session_id, "stopped")
        self.store.emit("session.stopped", session=session_id)
        return {"session_id": session_id, "state": "stopped"}


def _pixi_spec(dep: str) -> str:
    """'numpy >=2' -> 'numpy>=2' (pixi add wants no space)."""
    return dep.replace(" ", "")
