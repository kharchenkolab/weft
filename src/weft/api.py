"""The agent tool surface (doc 05 §1).

Compact, token-economical, asynchronous: no call blocks on queues or
transfers; submit returns a plan; results are manifests with previews;
errors are structured WeftError dicts. Every method returns plain
JSON-serializable data — this class *is* the MCP tool set, minus transport.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from .adapters.base import SiteAdapter
from .adapters.local import LocalAdapter
from .cas import LocalCAS
from .data import DataManager
from .envman import EnvManager
from .errors import WeftError
from .runner import JobRunner
from .store import Store
from .task import Task
from .transfer.local_link import LocalLink

DENY_PATTERNS = [
    re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*/(?!\S*weft)"),   # recursive rm outside weft root
    re.compile(r"\b(scontrol|sacctmgr)\s+(update|delete|modify)"),
    re.compile(r"\bmkfs\b|\bdd\s+.*of=/dev"),
    re.compile(r"\bshutdown\b|\breboot\b"),
]


class Weft:
    """One instance per workspace. The UI and the agent share this state."""

    def __init__(self, workspace: Path, pixi_bin: str | None = None,
                 pixi_pack: str | None = None):
        self.workspace = Path(workspace)
        data_dir = self.workspace / ".weft"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(data_dir / "state.db")
        self.cas = LocalCAS(data_dir / "cas")
        self.pixi_bin = pixi_bin or "pixi"
        if pixi_pack is None:
            sibling = Path(self.pixi_bin).parent / "pixi-pack"
            pixi_pack = str(sibling) if sibling.exists() else None
        self.pixi_pack = pixi_pack
        unpack_sibling = Path(self.pixi_bin).parent / "pixi-unpack"
        self.pixi_unpack = str(unpack_sibling) if unpack_sibling.exists() else None
        self.envman = EnvManager(self.store, data_dir / "solve", self.pixi_bin)
        self.dataman = DataManager(self.store, self.cas, self.workspace)
        self.adapters: dict[str, SiteAdapter] = {}
        from .transfer.rsync_ssh import RsyncSSH
        from .transfer.ssh_pipe import SshPipe
        self.transfers = {"local-link": LocalLink(), "rsync-ssh": RsyncSSH(),
                          "ssh-pipe": SshPipe()}
        self.runner = JobRunner(
            self.store, self.cas, self.envman, self.dataman,
            self.adapters, self.transfers, pixi_pack=self.pixi_pack,
        )
        self._module_cache: dict[tuple[str, str], bool] = {}
        from .session import SessionManager
        self.sessions = SessionManager(self.store, self.envman)
        self._restore_sites()

    # -- site management ---------------------------------------------------

    def _restore_sites(self) -> None:
        for row in self.store.list_sites():
            try:
                self._make_adapter(row["name"], row["kind"], row["config"])
            except WeftError:
                self.store.set_health(row["name"], "unreachable")

    def _make_adapter(self, name: str, kind: str, config: dict) -> SiteAdapter:
        if kind == "local":
            adapter = LocalAdapter(
                name, Path(config["root"]), pixi_source=config.get("pixi_source"),
            )
        elif kind == "ssh":
            from .adapters.ssh import SSHAdapter
            adapter = SSHAdapter(
                name, config["host"], config["root"],
                user=config.get("user"), port=config.get("port"),
                ssh_opts=config.get("ssh_opts"),
                pixi_source=config.get("pixi_source"),
                pixi_unpack_source=config.get("pixi_unpack_source", self.pixi_unpack),
            )
        elif kind == "slurm":
            from .adapters.slurm import SlurmAdapter
            sched = config.get("scheduler") or {}
            policy = config.get("policy") or {}
            adapter = SlurmAdapter(
                name, config["host"], config["root"],
                user=config.get("user"), port=config.get("port"),
                ssh_opts=config.get("ssh_opts"),
                pixi_source=config.get("pixi_source"),
                pixi_unpack_source=config.get("pixi_unpack_source", self.pixi_unpack),
                account=sched.get("account"),
                partition=sched.get("partition"),
                partitions_allowed=policy.get("partitions_allowed"),
                modules_init=config.get("modules_init", ""),
            )
        else:
            raise WeftError(
                "task.invalid", f"unknown site kind: {kind}", stage="infra",
                hints={"known": ["local", "ssh", "slurm"]},
            )
        self.adapters[name] = adapter
        return adapter

    def register_site(self, name: str, kind: str, config: dict) -> dict:
        """User-confirmed action (doc 05 §6): registering a site is always explicit."""
        adapter = self._make_adapter(name, kind, config)
        self.store.put_site(name, kind, config)
        adapter.ensure_bootstrap()
        probe = adapter.probe()
        from .capability import normalize_probe
        caps = self._apply_caps_override(normalize_probe(probe), config)
        self.store.set_capabilities(name, caps)
        self.store.audit_log("user", "site.register", site=name)
        self.store.emit("site.registered", site=name, site_kind=kind)
        return {"site": name, "capabilities": caps}

    def sites_list(self) -> list[dict]:
        out = []
        for row in self.store.list_sites():
            caps = row.get("capabilities") or {}
            entry = {
                "name": row["name"], "kind": row["kind"], "health": row["health"],
                "cpus": caps.get("cpus"), "mem_gb": caps.get("mem_gb"),
                "gpus": sum(g.get("count", 0) for g in caps.get("gpus", [])),
                "scheduler": (caps.get("scheduler") or {}).get("type"),
                "internet": caps.get("internet"),
            }
            from .policy import site_policy
            policy = site_policy(row)
            if policy:
                entry["policy"] = policy  # user rules + guidance notes
            out.append(entry)
        return out

    def sites_describe(self, name: str) -> dict:
        row = self.store.get_site(name)
        if not row:
            raise WeftError("task.invalid", f"unknown site: {name}", stage="infra",
                            hints={"registered": [s["name"] for s in self.store.list_sites()]})
        return row

    def site_probe(self, name: str) -> dict:
        """Re-probe on demand (capability drift, doc 02 §7)."""
        adapter = self._adapter(name)
        from .capability import normalize_probe
        config = (self.store.get_site(name) or {}).get("config", {})
        caps = self._apply_caps_override(normalize_probe(adapter.probe()), config)
        self.store.set_capabilities(name, caps)
        return caps

    @staticmethod
    def _apply_caps_override(caps: dict, config: dict) -> dict:
        """Site-config `capabilities_override` patches probed facts — for
        quirky sites the probe can't see through (and for tests that
        simulate e.g. air-gapped compute nodes)."""
        override = config.get("capabilities_override") or {}
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(caps.get(k), dict):
                caps[k] = {**caps[k], **v}
            else:
                caps[k] = v
        return caps

    def module_check(self, site: str, names: list[str]) -> dict:
        """Lazy per-site module inventory (doc 02 §3), cached."""
        adapter = self._adapter(site)
        if not hasattr(adapter, "module_avail"):
            return {"site": site, "supported": False,
                    "note": "site kind has no module system"}
        out = {}
        for n in names:
            key = (site, n)
            if key not in self._module_cache:
                self._module_cache[key] = adapter.module_avail(n)
            out[n] = self._module_cache[key]
        missing = [n for n, ok in out.items() if not ok]
        return {"site": site, "modules": out, "missing": missing,
                "satisfiable_here": not missing}

    def _adapter(self, name: str) -> SiteAdapter:
        if name not in self.adapters:
            raise WeftError("task.invalid", f"unknown site: {name}", stage="infra",
                            hints={"registered": sorted(self.adapters)})
        return self.adapters[name]

    # -- environments ---------------------------------------------------------

    def env_ensure(self, spec_or_id, *, update: bool = False) -> dict:
        try:
            return self.envman.ensure(spec_or_id, update=update)
        except WeftError as e:
            return e.to_dict()

    def env_status(self, env_id: str) -> dict:
        return self.envman.status(env_id)

    def env_gpu_hint(self, site: str) -> dict:
        """What GPU userland can this site's driver support? (doc S3/S4)"""
        from .gpu import suggest_gpu_spec
        row = self.sites_describe(site)
        return suggest_gpu_spec(row.get("capabilities") or {}, site)

    # -- data -----------------------------------------------------------------

    def data_register(self, path: str) -> dict:
        p = Path(path)
        if not p.is_absolute():
            p = self.workspace / p
        return self.dataman.register(p)

    def data_describe(self, ref: str) -> dict:
        return self.dataman.describe(ref)

    def data_fetch(self, ref: str, to_path: str) -> dict:
        return self.dataman.fetch(ref, to_path, self.adapters, self.transfers)

    # -- tasks ------------------------------------------------------------------

    def task_submit(self, task: dict, *, force: bool = False, dry_run: bool = False) -> dict:
        try:
            task = dict(task.get("task", task))
            if isinstance(task.get("env"), dict):
                # inline spec: resolve to an EnvID first (doc 01 §2)
                task["env"] = self.envman.ensure(task["env"])["env_id"]
            t = Task.from_dict(task)
            r = self.runner.submit(t, force=force, dry_run=dry_run)
            self.store.audit_log("agent", "task.submit",
                                 site=r.get("site", ""), command=t.command[:200])
            return r
        except WeftError as e:
            return e.to_dict()

    def task_status(self, job_id: str | None = None, state: str | None = None) -> list[dict]:
        jobs = [self.store.get_job(job_id)] if job_id else self.store.jobs_where(state=state)
        out = []
        for j in jobs:
            if j is None:
                continue
            out.append({
                "job_id": j["job_id"], "state": j["state"], "site": j["site"],
                "since": j["updated_at"],
                "error": j["error"],
                "has_manifest": j["manifest"] is not None,
            })
        return out

    def task_logs(self, job_id: str, tail: int = 100) -> str:
        job = self.store.get_job(job_id)
        if not job:
            raise WeftError("task.invalid", f"unknown job: {job_id}", stage="infra")
        adapter = self._adapter(job["site"])
        r = adapter.shim(
            ["tail", "--file", adapter.path(f"jobs/{job_id}/log"), "--lines", str(tail)],
            timeout=60,
        )
        return r.out

    def task_result(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        if not job:
            raise WeftError("task.invalid", f"unknown job: {job_id}", stage="infra")
        if job["state"] == "DONE":
            return job["manifest"]
        if job["state"] == "FAILED":
            return {"state": "FAILED", **(job["error"] or {})}
        return {"state": job["state"], "note": "not terminal yet — poll events"}

    def task_cancel(self, job_id: str) -> dict:
        self.store.audit_log("agent", "task.cancel", command=job_id)
        return self.runner.cancel(job_id)

    # -- session environments (doc 03 §7) --------------------------------------

    def session_start(self, env_id: str, site: str) -> dict:
        try:
            return self.sessions.start(env_id, self._adapter(site))
        except WeftError as e:
            return e.to_dict()

    def _session_adapter(self, session_id: str):
        s = self.store.get_session(session_id)
        if not s:
            raise WeftError("task.invalid", f"unknown session {session_id}", stage="infra")
        return self._adapter(s["site"])

    def session_exec(self, session_id: str, cmd: str) -> dict:
        return self.sessions.exec(session_id, self._session_adapter(session_id), cmd)

    def session_install(self, session_id: str, conda: list[str] | None = None,
                        pypi: list[str] | None = None) -> dict:
        try:
            return self.sessions.install(
                session_id, self._session_adapter(session_id), conda, pypi
            )
        except WeftError as e:
            return e.to_dict()

    def session_snapshot(self, session_id: str, name: str | None = None) -> dict:
        try:
            return self.sessions.snapshot(session_id, name)
        except WeftError as e:
            return e.to_dict()

    def session_stop(self, session_id: str) -> dict:
        return self.sessions.stop(session_id, self._session_adapter(session_id))

    # -- events / diagnostics -----------------------------------------------------

    def events_poll(self, since_cursor: int = 0, limit: int = 100) -> dict:
        events = self.store.events_since(since_cursor, limit)
        return {
            "events": events,
            "cursor": events[-1]["seq"] if events else since_cursor,
        }

    def site_exec(self, name: str, cmd: str, why: str) -> dict:
        """Guarded diagnostic shell (doc 05 §5): audited, deny-listed, scoped."""
        if not why or not why.strip():
            raise WeftError(
                "task.invalid", "site_exec requires a non-empty why", stage="infra",
                hints={"reason": "every diagnostic command is audited with its rationale"},
            )
        for pat in DENY_PATTERNS:
            if pat.search(cmd):
                self.store.audit_log("agent", "site.exec.DENIED", site=name,
                                     command=cmd, why=why)
                raise WeftError(
                    "task.invalid",
                    "command matches the deny list; ask the user to run it manually",
                    stage="infra", hints={"pattern": pat.pattern},
                )
        adapter = self._adapter(name)
        scoped = f"cd {shlex.quote(adapter.root)} && ( {cmd} )"
        r = adapter.run_cmd(scoped, timeout=120)
        self.store.audit_log("agent", "site.exec", site=name, command=cmd,
                             why=why, result=f"rc={r.rc}")
        return {"rc": r.rc, "stdout": r.out[-8000:], "stderr": r.err[-4000:],
                "cwd": adapter.root}

    def doctor(self) -> dict:
        """Self-diagnostics: the agent's first leverage point when confused."""
        checks = []
        for name, adapter in self.adapters.items():
            try:
                v = adapter.shim(["version"], timeout=15).json()
                checks.append({"site": name, "shim": v.get("shim_version"), "ok": True})
            except Exception as e:
                checks.append({"site": name, "ok": False, "error": str(e)[:200]})
                self.store.set_health(name, "unreachable")
        pending = self.store.nonterminal_jobs()
        return {
            "sites": checks,
            "nonterminal_jobs": [
                {"job_id": j["job_id"], "state": j["state"], "site": j["site"]}
                for j in pending
            ],
            "suggestion": "call reconcile() if nonterminal jobs look stale"
            if pending else None,
        }

    def reconcile(self) -> list[dict]:
        return self.runner.reconcile()
