"""The agent tool surface (doc 05 §1).

Compact, token-economical, asynchronous: no call blocks on queues or
transfers; submit returns a plan; results are manifests with previews;
errors are structured WeftError dicts. Every method returns plain
JSON-serializable data — this class *is* the MCP tool set, minus transport.
"""

from __future__ import annotations

import re
import shlex
import time  # noqa: F401  (site_load cache timestamps)
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

def tool(fn):
    """The API contract (uniform, by rule): every public Weft method
    returns JSON-shaped data; failures come back as error payloads
    (WeftError.to_dict()); nothing raises across this boundary. Internals
    raise WeftError freely — this decorator is the boundary."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except WeftError as e:
            return e.to_dict()
    wrapper._weft_tool = True
    return wrapper


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
        # cloud provisioner factories: name -> (site_config) -> CloudProvisioner.
        # "skypilot" is the intended production entry; tests register mocks.
        self.provisioners: dict = {}
        from .session import SessionManager
        self.sessions = SessionManager(self.store, self.envman)
        from .kernel import KernelManager
        self.kernels = KernelManager(self.store, self.adapters, self.runner)
        from .service import ServiceManager
        self.services = ServiceManager(self.store, self.adapters,
                                       self.runner, self.dataman)
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
        elif kind == "cloud":
            from .adapters.cloud import CloudAdapter
            prov_name = config.get("provisioner", "skypilot")
            factory = self.provisioners.get(prov_name)
            if factory is None:
                raise WeftError(
                    "task.invalid",
                    f"no provisioner {prov_name!r} registered",
                    stage="infra",
                    hints={"registered": sorted(self.provisioners)},
                )
            adapter = CloudAdapter(
                name, factory(config),
                budget=config.get("budget"),
                synthetic_caps=config.get("resources") or {},
                pixi_source=config.get("pixi_source"),
                pixi_unpack_source=config.get("pixi_unpack_source", self.pixi_unpack),
                emit=self.store.emit,
            )
        else:
            raise WeftError(
                "task.invalid", f"unknown site kind: {kind}", stage="infra",
                hints={"known": ["local", "ssh", "slurm", "cloud"]},
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

    def site_load(self, name: str, resources: dict | None = None,
                  fresh: bool = False) -> dict:
        """What's realistically available under current load (doc 02 §7 spirit):
        host load; on schedulers also idle CPUs, queue backlog, QOS, and —
        when `resources` is given — an sbatch --test-only start estimate."""
        adapter = self._adapter(name)
        if getattr(adapter, "launched", None) is False:
            return {"site": name, "note": "cloud instance not launched; "
                    "no live load (launching to measure would cost money)"}
        info = self.runner.get_load(name, fresh=fresh)
        if info is None:
            raise WeftError("site.unreachable",
                            f"could not read live load from {name}",
                            stage="infra", retryable=True)
        out = {"site": name, **info}
        if resources and hasattr(adapter, "estimate_start"):
            out["start_estimate"] = adapter.estimate_start(resources)
        return out

    def module_check(self, site: str, names: list[str]) -> dict:
        """Lazy per-site module inventory (doc 02 §3), cached."""
        adapter = self._adapter(site)
        if not hasattr(adapter, "module_avail"):
            return {"site": site, "module_system": "absent",
                    "note": "site kind has no module system"}
        system = adapter.module_system_status() \
            if hasattr(adapter, "module_system_status") else "ok"
        if system != "ok":
            return {"site": site, "module_system": system,
                    "modules": {n: False for n in names},
                    "satisfiable_here": False,
                    "note": "the `module` command is not available in "
                            "non-interactive shells here — set the site's "
                            "modules_init config (init script / MODULEPATH) "
                            "before trusting any 'missing' verdicts"}
        out = {}
        for n in names:
            key = (site, n)
            if key not in self._module_cache:
                self._module_cache[key] = adapter.module_avail(n)
            out[n] = self._module_cache[key]
        missing = [n for n, ok in out.items() if not ok]
        return {"site": site, "module_system": "ok", "modules": out,
                "missing": missing, "satisfiable_here": not missing}

    def _adapter(self, name: str) -> SiteAdapter:
        if name not in self.adapters:
            raise WeftError("task.invalid", f"unknown site: {name}", stage="infra",
                            hints={"registered": sorted(self.adapters)})
        return self.adapters[name]

    # -- environments ---------------------------------------------------------

    def env_ensure(self, spec_or_id, *, update: bool = False,
                   dry_run: bool = False) -> dict:
        try:
            return self.envman.ensure(spec_or_id, update=update,
                                      dry_run=dry_run)
        except WeftError as e:
            return e.to_dict()

    def env_status(self, env_id: str) -> dict:
        return self.envman.status(env_id)

    def env_why(self, env_id: str, package: str) -> dict:
        """Reverse-dependency probe: what pulls `package` in, per layer."""
        row = self.store.get_env(env_id)
        if not row:
            raise WeftError("task.invalid", f"unknown EnvID: {env_id}",
                            stage="solve")
        for eco, layer in (row["canonical"].get("layers") or {}).items():
            for rec in layer.get("records", []):
                if rec.get("name", "").lower() == package.lower():
                    return {"ecosystem": eco, "record": rec,
                            "explanation": f"{package} is in the {eco} layer "
                                           f"({rec.get('version')})"}
        why = self.envman.solvers["conda"].why(
            row, package, self.workspace / ".weft" / "why")
        return {"ecosystem": "conda", "explanation": why}

    def env_ensure_dry_run(self, spec: dict) -> dict:
        try:
            return self.envman.ensure(spec, dry_run=True)
        except WeftError as e:
            return e.to_dict()

    def env_repair(self, env_id: str, site: str) -> dict:
        """Force-rebuild lever: clears a realization the marker still claims
        (corrupt unpack, half-deleted prefix). The next task using this env
        on the site rebuilds it from the lockfile."""
        adapter = self._adapter(site)
        from .realize import env_dir_rel
        rel = env_dir_rel(env_id)
        adapter.run_cmd(f"rm -rf {shlex.quote(adapter.path(rel))}")
        real = self.store.get_realization(env_id, site)
        self.store.set_realization(
            env_id, site, (real or {}).get("strategy") or "prefix",
            rel, "missing", log="repair requested",
        )
        self.store.audit_log("agent", "env.repair", site=site, command=env_id)
        self.store.emit("env.repair", env_id=env_id, site=site)
        return {"env_id": env_id, "site": site, "state": "cleared",
                "note": "next task using this env here rebuilds it from the lockfile"}

    def env_gpu_hint(self, site: str) -> dict:
        """What GPU userland can this site's driver support? (doc S3/S4)"""
        from .gpu import suggest_gpu_spec
        row = self.sites_describe(site)
        return suggest_gpu_spec(row.get("capabilities") or {}, site)

    # -- data -----------------------------------------------------------------

    def data_register(self, path: str, site: str | None = None,
                      expected_sha256: str | None = None) -> dict:
        """Hash a workspace path — or ingest a URL (http/s/s3/gs/azure) —
        into a DataRef. With site=, a URL is fetched straight into that
        site's CAS (hashed site-side; no controller detour). Pass
        expected_sha256 to verify against a published checksum; otherwise
        hash-on-arrival is the identity (meta.trust = "first-fetch")."""
        if "://" in path:
            if not hasattr(self, "_fetchers"):
                from .sources import default_fetchers
                rclone = Path(self.pixi_bin).parent / "rclone"
                self._fetchers = default_fetchers(
                    str(rclone) if rclone.exists() else None)
            return self.dataman.register_url(
                path, self._fetchers, self.adapters, site=site,
                expected_sha256=expected_sha256)
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
            entry = {
                "job_id": j["job_id"], "state": j["state"], "site": j["site"],
                "since": j["updated_at"],
                "error": j["error"],
                "has_manifest": j["manifest"] is not None,
            }
            if j["state"] == "QUEUED" and j.get("queue_reason"):
                entry["queue_reason"] = j["queue_reason"]
            out.append(entry)
        return out

    def task_logs(self, job_id: str, tail: int = 100,
                  follow_cursor: int | None = None) -> dict:
        """Job log access. tail=N gives the last N lines. For live
        following pass follow_cursor (0 to start): returns new bytes since
        that offset plus the next cursor — poll while state is RUNNING."""
        job = self.store.get_job(job_id)
        if not job:
            raise WeftError("task.invalid", f"unknown job: {job_id}", stage="infra")
        adapter = self._adapter(job["site"])
        if job.get("array_group") and job.get("array_index") is not None \
                and "_" in (job.get("sched_handle") or ""):
            logrel = f"jobs/{job['array_group']}/el{job['array_index']}/log"
        else:
            logrel = f"jobs/{job_id}/log"
        if follow_cursor is not None:
            r = adapter.shim(
                ["read-from", "--file", adapter.path(logrel),
                 "--offset", str(follow_cursor), "--max", "65536"],
                timeout=60)
            chunk = r.out
            return {"log": chunk,
                    "cursor": follow_cursor + len(chunk.encode()),
                    "state": job["state"]}
        r = adapter.shim(
            ["tail", "--file", adapter.path(logrel), "--lines", str(tail)],
            timeout=60,
        )
        return {"log": r.out, "state": job["state"]}

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

    # -- kernels (persistent interactive interpreters) --------------------------

    def kernel_start(self, site: str, lang: str = "python",
                     env_id: str | None = None,
                     walltime: str = "08:00:00") -> dict:
        try:
            r = self.kernels.start(site, lang, env_id, walltime)
            self.store.audit_log("agent", "kernel.start", site=site,
                                 command=f"{lang} env={env_id}")
            return r
        except WeftError as e:
            return e.to_dict()

    def kernel_exec(self, kernel_id: str, code: str, wait: bool = True,
                    timeout: float = 120.0) -> dict:
        """Run a code block against the kernel's persistent state.
        wait=True returns {rc, out, err, artifacts}; wait=False returns a
        block handle for kernel_poll (long computations stay watchable)."""
        try:
            return self.kernels.exec(kernel_id, code, wait=wait, timeout=timeout)
        except WeftError as e:
            return e.to_dict()

    def kernel_poll(self, kernel_id: str, block: int,
                    timeout: float = 0.0) -> dict:
        """Check an async block: {"state": "running"} or the full result."""
        return self.kernels.poll(kernel_id, block, timeout=timeout)

    def kernel_status(self, kernel_id: str) -> dict:
        """State (running/died/stopped), current block + blocks_run, idle_s."""
        return self.kernels.status(kernel_id)

    def kernel_transcript(self, kernel_id: str, last: int = 20) -> list[dict]:
        """Ordered (code, rc, output tail) — the exploration's paper trail;
        the raw material for assembling the citable task."""
        return self.kernels.transcript(kernel_id, last)

    def kernel_interrupt(self, kernel_id: str) -> dict:
        """SIGINT the running block (finishes rc=130); interpreter survives."""
        return self.kernels.interrupt(kernel_id)

    def kernel_restart(self, kernel_id: str, replay: str = "successful") -> dict:
        try:
            return self.kernels.restart(kernel_id, replay)
        except WeftError as e:
            return e.to_dict()

    def kernel_stop(self, kernel_id: str) -> dict:
        return self.kernels.stop(kernel_id)

    # -- services (endpoint-publishing long-lived processes) ----------------------

    def service_start(self, site: str, task: dict, ports: list[int],
                      ready_timeout: float = 60.0) -> dict:
        """Start a long-lived process whose result is a live endpoint (a
        dashboard, notebook server, colocated query API). The command must
        bind 127.0.0.1 on $WEFT_PORT; weft tunnels it back and returns
        local URLs. Same env/staging/provenance as tasks; runs until
        service_stop (or its walltime)."""
        self.store.audit_log("agent", "service.start", site=site,
                             command=str(task.get("command", ""))[:200])
        return self.services.start(site, task, ports, ready_timeout)

    def service_status(self, service_id: str) -> dict:
        """State + live endpoints (tunnels re-established if needed —
        also re-hooks monitoring after a controller restart)."""
        return self.services.status(service_id)

    def service_stop(self, service_id: str, collect: bool = False) -> dict:
        """Stop the service and close its tunnels. collect=True harvests
        the task's declared outputs into refs (the service's side-products
        enter the record)."""
        self.store.audit_log("agent", "service.stop", command=service_id)
        return self.services.stop(service_id, collect=collect)

    # -- events / diagnostics -----------------------------------------------------

    def events_subscribe(self, callback) -> dict:
        """In-process push: callback receives every event object the poll
        feed would yield, as it is emitted (fire-and-forget; exceptions in
        the callback are swallowed). events_poll remains the canonical
        catch-up path — mix push for liveness with poll after gaps."""
        self.store.subscribe(callback)
        return {"subscribed": True,
                "note": "same objects as events_poll; poll for catch-up"}

    def events_poll(self, since_cursor: int = 0, limit: int = 100,
                    compact: bool = True) -> dict:
        """compact drops per-element events of array groups — the digests
        (array.progress / array.done) summarize them; a 2000-point scan
        reads as a handful of lines, not 2000. UIs pass compact=False."""
        events = self.store.events_since(since_cursor, limit)
        cursor = events[-1]["seq"] if events else since_cursor
        if compact:
            events = [e for e in events
                      if not (e.get("array_group")
                              and e["kind"].startswith("job."))]
        return {"events": events, "cursor": cursor}

    def array_status(self, group: str) -> dict:
        counts = self.store.group_counts(group)
        if counts["total"] == 0:
            raise WeftError("task.invalid", f"unknown array group: {group}",
                            stage="infra")
        return {"group": group, **counts,
                "failed_previews": self.store.failed_in_group(group),
                "elements": [
                    {"index": j["array_index"], "job_id": j["job_id"],
                     "state": j["state"],
                     # a memoized element's manifest names its original job
                     **({"memoized": True} if j["manifest"] and
                        j["manifest"].get("job_id") != j["job_id"] else {})}
                    for j in self.store.jobs_in_group(group)]}

    def array_retry(self, group: str, indices: list[int] | None = None,
                    command_override: str | None = None) -> dict:
        """Retry failed elements of an array group (or the given indices).
        Retries rejoin the group under their index — digests and the
        roll-up update; the superseded rows leave the group's counts."""
        jobs = self.store.jobs_in_group(group)
        if not jobs:
            raise WeftError("task.invalid", f"unknown array group: {group}",
                            stage="infra")
        want = set(indices) if indices is not None else None
        targets = [j for j in jobs
                   if (want is not None and j["array_index"] in want)
                   or (want is None and j["state"] == "FAILED")]
        if not targets:
            return {"group": group, "retried": [],
                    "note": "nothing to retry (no failed elements matched)"}
        from .task import Task
        with self.runner._digest_lock:
            self.runner._done_digests.discard(group)  # allow a new array.done
        out = []
        for j in targets:
            task = dict(j["task"])
            if command_override:
                task["command"] = command_override
            self.store.detach_from_group(j["job_id"])
            r = self.runner.submit(Task.from_dict(task), force=True,
                                   _group=group, _index=j["array_index"])
            out.append({"index": j["array_index"], "superseded": j["job_id"],
                        **{k: r[k] for k in ("job_id", "site") if k in r}})
        self.store.audit_log("agent", "array.retry", command=group,
                             why=f"{len(out)} elements")
        return {"group": group, "retried": out}

    def array_result(self, group: str) -> dict:
        counts = self.store.group_counts(group)
        if counts["total"] == 0:
            raise WeftError("task.invalid", f"unknown array group: {group}",
                            stage="infra")
        return {"group": group, **self.runner.group_rollup(group)}

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
        idle_kernels = [
            {"kernel_id": k["kernel_id"], "site": k["site"], "lang": k["lang"],
             "idle_s": round(time.time() - k["last_used"], 0)}
            for k in self.store.list_kernels(state="running")
            if time.time() - k["last_used"] > 3600
        ]
        events_n = self.store.events_count()
        return {
            "sites": checks,
            "nonterminal_jobs": [
                {"job_id": j["job_id"], "state": j["state"], "site": j["site"]}
                for j in pending
            ],
            "idle_kernels": idle_kernels or None,
            "events_rows": events_n if events_n > 100_000 else None,
            "suggestion": "call reconcile() if nonterminal jobs look stale"
            if pending else
            ("stop idle kernels (or set policy.kernel_idle_stop_s)"
             if idle_kernels else None),
        }

    def reconcile(self) -> list[dict]:
        return self.runner.reconcile()

    # -- garbage collection / retention ------------------------------------------

    def gc_plan(self, site: str | None = None) -> dict:
        """Dry-run eviction plan per site: idle realizations + stale cached
        refs (with pin status) and reclaimable bytes. Free to call; nothing
        is deleted."""
        from . import gc as _gc
        return _gc.plan(self, site)

    def gc_sweep(self, site: str, confirm: bool = False) -> dict:
        """Execute the eviction plan for a site — only with confirm=True
        (nothing on a shared system is deleted implicitly, doc 03 §6).
        Evicted content re-stages/rebuilds automatically on next use."""
        from . import gc as _gc
        return _gc.sweep(self, site, confirm=confirm)

    def gc_events(self, older_than_days: float = 30) -> dict:
        """Prune old events (terminal digests and failures are kept)."""
        pruned = self.store.prune_events(older_than_days)
        self.store.audit_log("user", "gc.events", result=f"pruned={pruned}")
        return {"pruned": pruned, "remaining": self.store.events_count()}

    # -- provenance -------------------------------------------------------------

    def provenance(self, target: str, depth: int = 5) -> dict:
        """The full "how was this produced" chain for a job or a DataRef:
        command + exact env identity (spec, locked layers, snapshot dates,
        pinned SHAs, attested modules) + input refs, recursing into the
        jobs that produced those inputs. Everything needed for a methods
        appendix, machine-readable."""
        if target.startswith("dref:"):
            row = self.store.get_dataref(target)
            if not row:
                raise WeftError("data.missing", f"unknown ref: {target}",
                                stage="infra")
            origin = row["meta"].get("origin", "")
            node = {"ref": target, "bytes": row["bytes"], "origin": origin}
            if origin.startswith("job:jobs/") and depth > 0:
                node["produced_by"] = self.provenance(
                    origin.split("/", 1)[1], depth - 1)
            return node

        job = self.store.get_job(target)
        if not job:
            raise WeftError("task.invalid", f"unknown job: {target}",
                            stage="infra")
        task = job["task"]
        node = {
            "schema": "provenance:v1",
            "reproducibility": (job["manifest"] or {}).get(
                "reproducibility", "task"),
            "job_id": target, "state": job["state"], "site": job["site"],
            "task_hash": job["task_hash"],
            "command": task.get("command"),
            "env_vars": task.get("env_vars") or {},
            "outputs": [{"path": o["path"], "ref": o["ref"]}
                        for o in (job["manifest"] or {}).get("outputs", [])],
        }
        env_id = task.get("env")
        if env_id:
            env = self.store.get_env(env_id)
            if env:
                extras = env["canonical"]["extras"]
                spec = self.store.get_spec(env["spec_hash"])
                node["environment"] = {
                    "env_id": env_id, "spec": spec,
                    "weakly_reproducible": env["weakly_reproducible"],
                    "modules_attested": extras.get("modules") or [],
                    "post_install": extras.get("post_install") or [],
                    "layers": {
                        eco: {"packages": len(l.get("records", [])),
                              "snapshot": l.get("snapshot"),
                              "pinned_shas": {
                                  r["name"]: r["remote_sha"]
                                  for r in l.get("records", [])
                                  if r.get("remote_sha")}}
                        for eco, l in (env["canonical"].get("layers")
                                       or {}).items()},
                }
        inputs = list(task.get("inputs") or [])
        if task.get("code"):
            inputs.append(task["code"])
        node["inputs"] = [
            {"mount_as": i["mount_as"],
             **(self.provenance(i["ref"], depth - 1) if depth > 0
                else {"ref": i["ref"]})}
            for i in inputs
        ]
        return node

    def site_teardown(self, name: str) -> dict:
        """Tear down an ephemeral (cloud) site's instance. Explicit —
        spending-level actions are never implicit (doc 05 §6)."""
        adapter = self._adapter(name)
        if not hasattr(adapter, "teardown"):
            return {"site": name, "note": "not an ephemeral site; nothing to do"}
        self.store.audit_log("user", "site.teardown", site=name)
        adapter.teardown()
        return {"site": name, "state": "terminated"}


# The public tool surface: uniformly wrapped (returns, never raises) and
# exactly what the MCP server exposes. One list, one source of truth.
PUBLIC_TOOLS = [
    "register_site", "sites_list", "sites_describe", "site_probe",
    "site_load", "module_check", "site_exec", "site_teardown",
    "env_ensure", "env_status", "env_why", "env_repair", "env_gpu_hint",
    "data_register", "data_describe", "data_fetch",
    "task_submit", "task_status", "task_logs", "task_result", "task_cancel",
    "array_status", "array_result", "array_retry",
    "events_poll", "doctor", "reconcile", "provenance",
    "gc_plan", "gc_sweep", "gc_events",
    "session_start", "session_exec", "session_install", "session_snapshot",
    "session_stop",
    "kernel_start", "kernel_exec", "kernel_poll", "kernel_status",
    "kernel_transcript", "kernel_interrupt", "kernel_restart", "kernel_stop",
    "service_start", "service_status", "service_stop",
]

for _name in PUBLIC_TOOLS:
    setattr(Weft, _name, tool(getattr(Weft, _name)))
