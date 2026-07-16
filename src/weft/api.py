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
                 pixi_pack: str | None = None, default_actor: str = "agent"):
        """default_actor names who acts through THIS instance in the audit
        trail ("agent" unless the embedder — a UI serving a human, a
        notebook — says otherwise). Deliberately constructor-only: a
        per-call actor on the tools would let an agent write someone
        else's name into the trail. Registration-class actions always
        audit as "user" (they are user-confirmed by doctrine)."""
        self.workspace = Path(workspace)
        data_dir = self.workspace / ".weft"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(data_dir / "state.db")
        self.store.audit_actor = default_actor
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
        self.sessions = SessionManager(self.store, self.envman, self.runner,
                                       self.dataman, self.adapters)
        from .kernel import KernelManager
        self.kernels = KernelManager(self.store, self.adapters, self.runner,
                                     sessions=self.sessions)
        from .retain import RetainManager
        self.retains = RetainManager(self.store, self.adapters,
                                     self.workspace)
        # settlement hooks (job collect, kernel stop/death) capture
        # pinned-pending retains through this reference
        self.runner.retains = self.retains
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
                shared=bool(config.get("shared")),
                pixi_cache=config.get("pixi_cache"),
            )
        elif kind == "ssh":
            from .adapters.ssh import SSHAdapter
            adapter = SSHAdapter(
                name, config["host"], config["root"],
                user=config.get("user"), port=config.get("port"),
                ssh_opts=config.get("ssh_opts"),
                jump=config.get("jump"),
                pixi_source=config.get("pixi_source"),
                pixi_unpack_source=config.get("pixi_unpack_source", self.pixi_unpack),
                shared=bool(config.get("shared")),
                pixi_cache=config.get("pixi_cache"),
            )
        elif kind == "slurm":
            from .adapters.slurm import SlurmAdapter
            sched = config.get("scheduler") or {}
            policy = config.get("policy") or {}
            # no host = the controller IS the submit node (sbatch on
            # PATH, GSSAPI-only ssh-to-self impossible on some sites) —
            # every command runs as a direct subprocess
            transport = config.get("transport") or \
                ("local" if not config.get("host") else "ssh")
            adapter = SlurmAdapter(
                name, config.get("host") or "localhost", config["root"],
                transport=transport,
                user=config.get("user"), port=config.get("port"),
                ssh_opts=config.get("ssh_opts"),
                jump=config.get("jump"),
                pixi_source=config.get("pixi_source"),
                pixi_unpack_source=config.get("pixi_unpack_source", self.pixi_unpack),
                account=sched.get("account"),
                partition=sched.get("partition"),
                partitions_allowed=policy.get("partitions_allowed"),
                modules_init=config.get("modules_init", ""),
                extra_directives=sched.get("extra_directives"),
                shared=bool(config.get("shared")),
                pixi_cache=config.get("pixi_cache"),
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

    def register_site(self, name: str, kind: str, config: dict,
                      probe_only: bool = False) -> dict:
        """User-confirmed action (doc 05 §6): registering a site is always
        explicit. probe_only=True bootstraps and probes WITHOUT
        registering (no store row, no routes, no site tooling) — the
        wizard's check-before-commit. Honest caveat: the real probe needs
        the shim, so ~100KB is written under the configured root even
        then (re-used by the eventual registration)."""
        adapter = self._make_adapter(name, kind, config)
        if not probe_only:
            self.store.put_site(name, kind, config)
        # progress is narrated: registration can take a minute on real
        # hosts (bootstrap push, probe, tool fetch, route probes)
        self.store.emit("bootstrap.step", site=name, step="bootstrap",
                        note="creating the root tree, pushing the shim")
        adapter.ensure_bootstrap()
        self.store.emit("bootstrap.step", site=name, step="probe",
                        note="measuring the site (hardware, scheduler, "
                             "runtimes, network)")
        probe = adapter.probe()
        from .capability import normalize_probe
        caps = self._apply_caps_override(normalize_probe(probe), config)
        if probe_only:
            self.adapters.pop(name, None)
            return {"site": name, "probe_only": True, "capabilities": caps,
                    "note": "nothing registered — the shim was written "
                            "under the root to run a real probe; "
                            "register_site without probe_only to commit"}
        self.store.set_capabilities(name, caps)
        # the site needs pixi/pixi-unpack built for ITS platform; push the
        # controller's copy when compatible, else fetch the pinned release
        # (best-effort: bare tasks run without them; realize hints name this)
        if hasattr(adapter, "_push_binary"):
            from .realize import _site_platform
            from .site_tools import ensure_site_tools
            self.store.emit("bootstrap.step", site=name, step="tools",
                            note="ensuring platform-correct pixi/pixi-unpack")
            try:
                tools = ensure_site_tools(adapter, _site_platform(caps))
                self.store.emit("site.tools", site=name, **tools)
            except Exception as e:  # never fail a registration on tooling
                self.store.emit("site.tools", site=name,
                                error=str(e)[:200])
        self.store.audit_log("user", "site.register", site=name)
        self.store.emit("site.registered", site=name, site_kind=kind)
        # discover byte routes to/from the other sites (best-effort: a
        # failed probe never fails a registration)
        if len(self.adapters) > 1:
            self.store.emit("bootstrap.step", site=name, step="routes",
                            note="probing byte routes to the other sites")
        for other in list(self.adapters):
            if other == name:
                continue
            for s, d in ((other, name), (name, other)):
                try:
                    self.site_route_probe(s, d)
                except Exception:
                    pass
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
        notes = self.store.site_notes(name)
        if notes:
            row = {**row, "site_notebook": notes}
        routes = self.store.routes_for(name)
        if routes:
            row = {**row, "routes": [
                {"src": r["src"], "dst": r["dst"],
                 "via": "shared-fs" if r["shared_fs_path"]
                 else ("direct-ssh" if r["direct_ssh"] else "controller")}
                for r in routes]}
        return row

    def site_route_probe(self, src: str, dst: str) -> dict:
        """Discover how bytes can move src→dst WITHOUT the controller in
        the middle: (a) shared filesystem — a nonce written under src's
        root visible from dst at the same path (group NFS, cluster
        scratch); (b) direct ssh — dst can already reach src with its own
        keys (weft brokers no identity; this only DISCOVERS what the
        user's existing config permits). Recorded per (src, dst); staging
        plans route accordingly, controller detour as fallback."""
        import uuid as _uuid
        if src == dst:
            raise WeftError("task.invalid", "src and dst are the same site",
                            stage="infra")
        a, b = self._adapter(src), self._adapter(dst)
        shared_fs_path = None
        nonce = f".weft-fsprobe-{_uuid.uuid4().hex[:12]}"
        try:
            a.write_file(nonce, b"weft route probe\n")
            if hasattr(b, "file_exists") and \
                    b.file_exists(f"{a.root}/{nonce}"):
                shared_fs_path = str(a.root)
        finally:
            a.run_cmd(f"rm -f {shlex.quote(a.path(nonce))} 2>/dev/null; true")
        direct_ssh, src_addr = False, ""
        if shared_fs_path is None and hasattr(a, "destination"):
            # how PEERS reach src may differ from how the controller does
            # (NAT, port maps): peer_host/peer_port override in site config
            cfg = (self.store.get_site(src) or {}).get("config") or {}
            host = cfg.get("peer_host") or cfg.get("host")
            user = cfg.get("user")
            pport = cfg.get("peer_port") if cfg.get("peer_host")                 else cfg.get("port")
            dest = f"{user}@{host}" if user else str(host)
            port = f"-p {pport} " if pport else ""
            r = b.run_cmd(
                f"ssh -o BatchMode=yes -o ConnectTimeout=5 "
                f"-o StrictHostKeyChecking=accept-new {port}"
                f"{shlex.quote(dest)} true 2>/dev/null "
                f"&& echo weft-route-ok || true", timeout=30)
            direct_ssh = "weft-route-ok" in r.out
            if direct_ssh:
                src_addr = f"{dest}:{pport}" if pport else dest
        self.store.set_route(src, dst, shared_fs_path, direct_ssh, src_addr)
        self.store.audit_log(None, "site.route_probe",
                             site=dst, command=f"from {src}")
        out = {"src": src, "dst": dst, "shared_fs_path": shared_fs_path,
               "direct_ssh": direct_ssh}
        self.store.emit("site.route", **out)
        if shared_fs_path:
            out["note"] = ("dst sees src's root on a shared filesystem: "
                           "transfers become links/copies, no network")
        elif direct_ssh:
            out["note"] = ("dst can pull from src directly: transfers skip "
                           "the controller")
        else:
            out["note"] = "no direct route; transfers go via the controller"
        return out

    def site_note(self, name: str, note: str) -> dict:
        """Persist a per-site operational note ('gcc lives in ~/toolchains',
        'module load is broken on the gpu partition') — the knowledge that
        otherwise dies with the session. Notes ride along in
        sites_describe; newest last. Append-only and audited."""
        if not self.store.get_site(name):
            raise WeftError("task.invalid", f"unknown site: {name}",
                            stage="infra")
        if not note or not note.strip():
            raise WeftError("task.invalid", "empty note", stage="infra")
        self.store.add_site_note(name, note.strip())
        self.store.audit_log(None, "site.note", site=name,
                             command=note[:200])
        return {"site": name, "notes": self.store.site_notes(name)}

    def site_associations(self, name: str) -> dict:
        """What am *I* allowed to ask for on this scheduler: my accounts,
        allowed/default QOS per partition, structured QOS ceilings
        (cpu/gpu/mem per user), fairshare. Fields are None (never
        defaulted) when the cluster exposes no accounting."""
        adapter = self._adapter(name)
        if not hasattr(adapter, "associations"):
            return {"site": name,
                    "note": "not a scheduler site — no association model"}
        return {"site": name, **adapter.associations()}

    def site_probe_deep(self, name: str, partitions: list[str] | None = None,
                        wait_s: int = 180) -> dict:
        """COMPUTE-NODE truth: submit the shim's own probe as a minimal
        job per partition and record what the nodes actually are (GPUs,
        glibc, arch — and MEASURED egress: 'login has internet' says
        nothing about the nodes). Fills per-partition `compute` records in
        capabilities:v2; the default partition's record becomes the site's
        `compute` view, which realization strategy keys on."""
        import uuid as _uuid
        adapter = self._adapter(name)
        if not hasattr(adapter, "submit") or \
                not hasattr(adapter, "_probe_partitions"):
            return {"site": name,
                    "note": "not a scheduler site — the direct probe "
                            "already describes where jobs run"}
        row = self.store.get_site(name) or {}
        caps = row.get("capabilities") or {}
        known = {p["name"]: p
                 for p in (caps.get("scheduler") or {}).get("partitions", [])}
        targets = partitions or [n for n, p in known.items()
                                 if p.get("available", True)]
        results, submitted = {}, []
        for part in targets:
            jd = f"jobs/probe-{part}-{_uuid.uuid4().hex[:8]}"
            adapter.run_cmd(f"mkdir -p {shlex.quote(adapter.path(jd))}")
            # absolute path baked in: batch environments do NOT reliably
            # inherit the submission env (found on a real 26.05 cluster —
            # $WEFT_ROOT was empty inside the job)
            adapter.write_file(
                f"{jd}/cmd.sh",
                (shlex.quote(adapter.path("bin/weft-shim"))
                 + " probe > probe.json\n").encode())
            try:
                res = {"cpus": 1, "walltime": "00:05:00", "partition": part}
                if any(g.get("type") == "gpu"
                       for g in (known.get(part) or {}).get("gres") or []):
                    # cgroup device isolation hides GPUs from jobs that
                    # did not ask — a GPU partition's probe must ask
                    res["gpus"] = 1
                handle = adapter.submit(jd, {"command": "probe",
                                             "resources": res})
                submitted.append((part, jd, handle))
            except WeftError as e:
                results[part] = {"ok": False, "error": e.detail[:200]}
        deadline = time.time() + wait_s
        for part, jd, handle in submitted:
            jid = handle.split(":", 1)[-1]
            probe, finished = None, False
            while time.time() < deadline:
                if adapter.file_exists(f"{jd}/exit_code"):
                    finished = True
                    try:
                        import json as _json
                        probe = _json.loads(
                            adapter.read_file(f"{jd}/probe.json").decode())
                    except (ValueError, WeftError):
                        probe = None
                    break
                time.sleep(2)
            if probe is None:
                if finished:   # ran, but the probe itself broke — say so
                    log = ""
                    try:
                        log = adapter.read_file(f"{jd}/log", 2000).decode()
                    except WeftError:
                        pass
                    results[part] = {
                        "ok": False,
                        "error": "probe job ran but produced no usable "
                                 "probe output",
                        "log_tail": log[-500:]}
                    continue
                adapter.run_cmd(f"scancel {shlex.quote(jid)} 2>/dev/null; true")
                results[part] = {
                    "ok": False,
                    "note": f"no result within {wait_s}s (queue busy?) — "
                            "re-run when the partition drains"}
                continue
            from .capability import normalize_probe
            rec = normalize_probe(probe)
            if part in known:
                known[part]["compute"] = rec
            results[part] = {
                "ok": True, "node": rec["hostname"],
                "internet": rec["internet"], "glibc": rec["glibc"],
                "gpus": rec["gpus"], "cuda_driver": rec["cuda_driver"],
            }
            adapter.run_cmd(f"rm -rf {shlex.quote(adapter.path(jd))}")
        # the default (or first probed) partition's node record becomes
        # the site's compute view — what strategy selection reasons over
        for part in targets:
            rec = known.get(part, {}).get("compute")
            if rec:
                caps["compute"] = rec
                break
        self.store.set_capabilities(name, caps)
        self.store.emit("site.probed_deep", site=name,
                        partitions=list(results))
        return {"site": name, "partitions": results,
                "note": "per-partition compute records saved; realization "
                        "strategy now keys on MEASURED node egress"}

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
        if override:
            # declared-vs-measured provenance: an agent should know which
            # facts came from a human's claim rather than a probe
            caps["overridden_fields"] = sorted(override)
        return caps

    def site_load(self, name: str, resources: dict | None = None,
                  fresh: bool = False,
                  partitions: list[str] | None = None) -> dict:
        """What's realistically available under current load (doc 02 §7 spirit):
        host load; on schedulers also idle CPUs + GPUs, queue backlog, QOS,
        my associations/fairshare, and — when `resources` is given — an
        sbatch --test-only start estimate (per partition when `partitions`
        names candidates: 'shortest queue for this ask, right now')."""
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
            if partitions:
                out["start_estimates"] = {
                    p: adapter.estimate_start(resources, partition=p)
                    for p in partitions}
            else:
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

    def module_list(self, site: str, search: str | None = None,
                    limit: int = 200) -> dict:
        """Enumerate the site's module offerings (discovery — module_check
        is for verifying names you already know). Cached per site; pass
        search= to filter (case-insensitive substring)."""
        adapter = self._adapter(site)
        if not hasattr(adapter, "module_inventory"):
            return {"site": site, "module_system": "absent",
                    "note": "site kind has no module system"}
        system = adapter.module_system_status() \
            if hasattr(adapter, "module_system_status") else "ok"
        if system != "ok":
            return {"site": site, "module_system": system, "modules": [],
                    "note": "the `module` command is unavailable in "
                            "non-interactive shells — set modules_init in "
                            "the site config first"}
        key = ("__inventory__", site)
        if key not in self._module_cache:
            self._module_cache[key] = adapter.module_inventory()
        names = self._module_cache[key]
        if search:
            s = search.lower()
            names = [n for n in names if s in n.lower()]
        out = {"site": site, "module_system": "ok", "total": len(names),
               "modules": names[:limit]}
        if len(names) > limit:
            out["note"] = (f"showing {limit} of {len(names)} — refine with "
                           "search= (e.g. search='cuda')")
        return out

    def _adapter(self, name: str) -> SiteAdapter:
        if name not in self.adapters:
            raise WeftError("task.invalid", f"unknown site: {name}", stage="infra",
                            hints={"registered": sorted(self.adapters)})
        return self.adapters[name]

    # -- environments ---------------------------------------------------------

    def env_ensure(self, spec_or_id, update: bool = False,
                   dry_run: bool = False, relax: str = "none") -> dict:
        """Resolve a spec (or return a known EnvID). Mark a constraint SOFT
        with a trailing '?' ("scipy ==1.14.1?") and pass relax="soft" to get
        a working env in one call instead of a conflict-relax-retry loop:
        weft drops only soft constraints, reports what it relaxed, and the
        result is still fully pinned. Hard pins are never touched.
        dry_run=True solves without storing."""
        return self.envman.ensure(spec_or_id, update=update,
                                  dry_run=dry_run, relax=relax)

    def env_status(self, env_id: str) -> dict:
        return self.envman.status(env_id)

    def env_revise(self, env_id: str, reason: str = "") -> dict:
        """Reproduce-else-revise: when an EnvID can no longer be realized as
        recorded (package pulled, snapshot moved), re-solve its original spec
        and report the package-level diff. Mints a NEW EnvID — the old one
        stays valid as a record and nothing is silently redefined. Sites can
        do this automatically with policy `on_drift: "revise"`."""
        return self.envman.revise(env_id, reason)

    def env_find_near(self, spec: dict, site: str | None = None,
                      limit: int = 5) -> list[dict]:
        """Which already-solved (with site=, already-REALIZED) envs are close
        to this spec? A query, not a policy — weft never substitutes a
        near-match behind your back. You see the distance, the missing
        packages, the grade, and where it is warm; you decide whether to
        submit against it (instant) or solve fresh (exact)."""
        return self.envman.find_near(spec, site=site, limit=limit)

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
        real = self.store.get_realization(env_id, site)
        if real and real.get("read_only"):
            root = real["location"].rsplit("/envs/", 1)[0]
            # forget the adoption (ours to forget) — never touch the files
            # (not ours to fix)
            self.store.set_realization(env_id, site, real["strategy"],
                                       env_dir_rel(env_id), "missing",
                                       log="read-only adoption dropped")
            self.store.emit("env.repair", env_id=env_id, site=site,
                            read_only=True)
            return {"env_id": env_id, "site": site, "state": "cleared",
                    "note": "adoption from the read-only root dropped — the "
                            f"files under {root} belong to its owner and "
                            "were not touched. Next use re-verifies: a "
                            "healthy read-only copy is re-adopted; a broken "
                            "one is reported and a private copy builds in "
                            "your root."}
        rel = env_dir_rel(env_id)
        adapter.run_cmd(f"rm -rf {shlex.quote(adapter.path(rel))}")
        real = self.store.get_realization(env_id, site)
        self.store.set_realization(
            env_id, site, (real or {}).get("strategy") or "prefix",
            rel, "missing", log="repair requested",
        )
        self.store.audit_log(None, "env.repair", site=site, command=env_id)
        self.store.emit("env.repair", env_id=env_id, site=site)
        return {"env_id": env_id, "site": site, "state": "cleared",
                "note": "next task using this env here rebuilds it from the lockfile"}

    def env_gpu_hint(self, site: str) -> dict:
        """What GPU userland can this site's driver support? (doc S3/S4)"""
        from .gpu import suggest_gpu_spec
        row = self.sites_describe(site)
        return suggest_gpu_spec(row.get("capabilities") or {}, site)

    # -- published envs (institutional read-only trees) ---------------------

    def env_publish(self, env_id: str, site: str, tree: str, name: str,
                    version: str, notes: str = "",
                    latest: bool = True) -> dict:
        """Build env_id as a squashfs image at {tree}/envs/<hash> and
        point catalog[name][version] at it — the admin half of ro_roots.
        The tree must live OUTSIDE the weft root; the catalog stores the
        spec+lock so consumers adopt by NAME with no solving. Publish is
        a rebuild at the destination (conda envs bake absolute paths);
        the site package cache makes it cheap after a test build."""
        from . import publish as _p
        return _p.publish(self, env_id, site, tree, name, version,
                          notes=notes, latest=latest)

    def env_adopt(self, site: str, tree: str, name: str,
                  version: str = "latest") -> dict:
        """Resolve a published name→EnvID from {tree}/catalog.json and
        import its lock (no solve, no network). Use the returned env_id
        in task_submit/kernel_start; extends_env overlays stack on top.
        The site's ro_roots must include the tree for adoption-in-place."""
        from . import publish as _p
        return _p.adopt(self, site, tree, name, version=version)

    def env_unpublish(self, site: str, tree: str, name: str, version: str,
                      purge: bool = False) -> dict:
        """Retire a published version: the catalog pointer goes (no new
        adoptions), the directory STAYS for a grace period; purge=True
        deletes it. Consumers' integrity fences fail loudly afterwards —
        never silently."""
        from . import publish as _p
        return _p.unpublish(self, site, tree, name, version, purge=purge)

    def env_published(self, site: str, tree: str) -> dict:
        """List a tree's catalog as render-ready rows (published:v1):
        write-time facts (grade, spec_summary, glibc_floor, image bytes)
        plus read-time truth per version — is_latest, runnable_here,
        state_here (adopted-ro/ready/building/failed/missing), last_used."""
        from . import publish as _p
        return _p.published(self, site, tree)

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
        if site is not None:
            # a file already ON the site (e.g. retained in place):
            # hashed site-side, hardlinked into the site CAS — reuse on
            # that site never crosses the WAN (retention.md R5)
            adapter = self.adapters.get(site)
            if adapter is None:
                raise WeftError("task.invalid", f"unknown site: {site}",
                                stage="infra",
                                hints={"registered": sorted(self.adapters)})
            return self.dataman.register_site_path(
                adapter, site, path,
                origin=self._lineage_origin(path, site),
                expected_sha256=expected_sha256)
        p = Path(path)
        if not p.is_absolute():
            p = self.workspace / p
        return self.dataman.register(p, origin=self._lineage_origin(str(p)))

    def _lineage_origin(self, path: str, site: str | None = None) -> str | None:
        """A path under a retained tree carries its producing run as
        origin (`run:<target>/<relpath>`) — provenance() then recurses
        THROUGH the file instead of dead-ending on a path string."""
        for row in self.store.retained_where():
            if site is None and row["in_place"]:
                continue
            if site is not None and (not row["in_place"]
                                     or row["site"] != site):
                continue
            loc = row["location"].rstrip("/")
            if path.startswith(loc + "/"):
                return f"run:{row['target']}/{path[len(loc) + 1:]}"
        return None

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
            self.store.audit_log(None, "task.submit",
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
                "label": (j.get("task") or {}).get("label") or None,
                "since": j["updated_at"],
                "error": j["error"],
                "has_manifest": j["manifest"] is not None,
            }
            if j["state"] == "QUEUED" and j.get("queue_reason"):
                entry["queue_reason"] = j["queue_reason"]
            if job_id:  # single-job detail: the promise made at submit
                entry["plan"] = (self.store.get_plan(j["job_id"])
                                 or (self.store.get_plan(j["array_group"])
                                     if j.get("array_group") else None))
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

    def task_cancel(self, job_id: str, why: str = "") -> dict:
        """Cancel a job. Pass `why` — cancellations are part of the record
        ("hung: no output for 20 min, node memory exhausted"), and the
        cause is what makes the audit trail useful later."""
        self.store.audit_log(None, "task.cancel", command=job_id,
                             why=why)
        out = self.runner.cancel(job_id)
        if why:
            out["why"] = why
        return out

    # -- session environments (doc 03 §7) --------------------------------------

    def session_start(self, env_id, site: str) -> dict:
        """Start a mutable scratch environment for iteration. Accepts an
        EnvID *or an inline spec* — and realizes the base itself if needed,
        so exploration costs one call, not three."""
        return self.sessions.start(env_id, self._adapter(site))

    def _session_adapter(self, session_id: str):
        s = self.store.get_session(session_id)
        if not s:
            raise WeftError("task.invalid", f"unknown session {session_id}", stage="infra")
        return self._adapter(s["site"])

    def session_exec(self, session_id: str, cmd: str) -> dict:
        return self.sessions.exec(session_id, self._session_adapter(session_id), cmd)

    def session_install(self, session_id: str, conda: list[str] | None = None,
                        pypi: list[str] | None = None) -> dict:
        """Add packages to the session (fast: the site's package cache is
        warm). Captured, so a snapshot carries them into the spec."""
        return self.sessions.install(
            session_id, self._session_adapter(session_id), conda, pypi)

    def session_run_installer(self, session_id: str, cmd: str,
                              note: str = "", source: str | None = None) -> dict:
        """Run a bespoke install that no index expresses (R
        install.packages, pip install -e, a vendored make install). A normal
        move — it is captured and a snapshot carries it as a labeled
        post_install step (grade: escape-hatch). Pass `source=<local path>`
        if the command needs local files: weft content-addresses them so the
        step travels with the env and rebuilds ANYWHERE. `note` records why."""
        return self.sessions.run_installer(
            session_id, self._session_adapter(session_id), cmd, note, source)

    def session_snapshot(self, session_id: str, name: str | None = None,
                         notes: list[str] | None = None,
                         verify: bool = True) -> dict:
        """Freeze the session's additions into a real, citable EnvID (a
        fresh whole-spec solve). Captured installers ride along as labeled
        post_install steps with their sources content-addressed. With
        verify=True (default) weft REALIZES the minted env before handing it
        back — a citable EnvID that cannot be rebuilt is worse than an
        error. `notes` records the rationale."""
        return self.sessions.snapshot(session_id, name, notes, verify)

    def session_stop(self, session_id: str) -> dict:
        return self.sessions.stop(session_id, self._session_adapter(session_id))

    # -- kernels (persistent interactive interpreters) --------------------------

    def kernel_start(self, site: str, lang: str = "python",
                     env_id: str | None = None,
                     walltime: str = "08:00:00",
                     resources: dict | None = None,
                     label: str = "",
                     session_id: str | None = None,
                     capture: str = "transcript") -> dict:
        """resources={"gpus": 1, "partition": "gpu"} on a scheduler site
        holds a node allocation and runs the kernel INSIDE it — live
        interactive analysis on a GPU node; no ports, the shared
        filesystem is the channel. label ("phonon exploration") is a
        display handle, carried into status/lists/death events and
        inherited by kernel_restart's successor.

        session_id (mutually exclusive with env_id) attaches the kernel
        to a LIVE session prefix: session_install lands in the running
        kernel, visible to the next block. Promotion auto-snapshots the
        session into a real EnvID so the record never cites a moving
        target."""
        try:
            r = self.kernels.start(site, lang, env_id, walltime, resources,
                                   label=label, session_id=session_id,
                                   capture=capture)
            self.store.audit_log(None, "kernel.start", site=site,
                                 command=f"{lang} env={env_id or session_id}")
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

    def kernel_peek(self, kernel_id: str, block: int, out_offset: int = 0,
                    err_offset: int = 0, max_bytes: int = 65536) -> dict:
        """Incremental live output for a block: {out_delta, err_delta,
        out_offset, err_offset, running, rc}. Feed the returned offsets
        back in — works identically for local and remote kernels, so a
        host's streaming pane needs one code path."""
        return self.kernels.peek(kernel_id, block, out_offset, err_offset,
                                 max_bytes)

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

    def kernel_promote(self, kernel_id: str, blocks: list[int]) -> dict:
        """Promote successful kernel blocks into the record: a manifest
        with reproducibility="transcript" — the full ordered transcript
        (replayable) plus the blocks' artifacts as content-addressed
        outputs. Explicit and honestly labeled; the default doctrine
        (re-run as a task for "task"-grade reproducibility) is unchanged."""
        return self.kernels.promote(kernel_id, blocks, dataman=self.dataman)

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
        self.store.audit_log(None, "service.start", site=site,
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
        self.store.audit_log(None, "service.stop", command=service_id)
        return self.services.stop(service_id, collect=collect)

    # -- read-only enumeration (agent/UI parity: "what exists here?") ----------

    def jobs_where(self, state: str | None = None, site: str | None = None,
                   limit: int = 100, offset: int = 0) -> dict:
        """List job rows (oldest first), filterable by state and site.
        Rows carry array_group/array_index; a retried array element's old
        row carries `superseded_by` — fold those under the group's history
        rather than reading them as duplicates."""
        rows = self.store.jobs_where(state=state, site=site,
                                     limit=limit, offset=offset)
        return {"jobs": rows, "count": len(rows), "offset": offset,
                "limit": limit}

    def list_envs(self) -> dict:
        """Every solved environment in this workspace (newest first):
        env_id, spec name, platforms. env_status(env_id) has the rest."""
        return {"envs": self.store.list_envs()}

    def list_kernels(self, state: str | None = None) -> dict:
        """Kernels this workspace knows (optionally filtered by state,
        e.g. 'running'). kernel_status(kernel_id) gives live truth."""
        return {"kernels": self.store.list_kernels(state=state)}

    def list_services(self, state: str | None = None) -> dict:
        """Services this workspace knows (optionally filtered by state).
        service_status(service_id) re-checks endpoints and liveness."""
        return {"services": self.store.list_services(state=state)}

    def audit_tail(self, n: int = 50) -> dict:
        """The last n audited actions (user and agent share one trail)."""
        return {"audit": self.store.audit_tail(n)}

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

    _ARRAY_INLINE_CAP = 200

    def array_status(self, group: str) -> dict:
        """Counts + FAILURE BUCKETS (elements clustered by log signature —
        a 2000-element sweep with three failure modes reads as three lines,
        each with sample indices to drill into). The per-element list is
        inlined only for small groups; use array_elements to page."""
        counts = self.store.group_counts(group)
        if counts["total"] == 0:
            raise WeftError("task.invalid", f"unknown array group: {group}",
                            stage="infra")
        members = self.store.jobs_in_group(group, limit=1)
        out = {"group": group, **counts,
               "label": (members[0].get("task") or {}).get("label") or None
               if members else None,   # elements share the submit's label
               "plan": self.store.get_plan(group),  # the submit-time promise
               "failed_previews": self.store.failed_in_group(group),
               "failure_buckets": self._failure_buckets(group)}
        if counts["total"] <= self._ARRAY_INLINE_CAP:
            out["elements"] = [
                {"index": j["array_index"], "job_id": j["job_id"],
                 "state": j["state"],
                 # a memoized element's manifest names its original job
                 **({"memoized": True} if j["manifest"] and
                    j["manifest"].get("job_id") != j["job_id"] else {})}
                for j in self.store.jobs_in_group(group)]
        else:
            out["note"] = (f"{counts['total']} elements — page them with "
                           "array_elements(group, state=..., offset=, "
                           "limit=); failure_buckets summarize the failed "
                           "ones")
        return out

    def _failure_buckets(self, group: str) -> list[dict]:
        buckets: dict[str, dict] = {}
        for j in self.store.jobs_in_group(group, state="FAILED"):
            err = j.get("error") or {}
            sig = (((err.get("hints") or {}).get("log_signature") or {})
                   .get("signature")) or err.get("error") or "unknown"
            b = buckets.setdefault(sig, {"signature": sig, "count": 0,
                                         "sample_indices": [],
                                         "sample_job_id": j["job_id"]})
            b["count"] += 1
            if len(b["sample_indices"]) < 5:
                b["sample_indices"].append(j["array_index"])
        return sorted(buckets.values(), key=lambda b: -b["count"])

    def array_elements(self, group: str, state: str | None = None,
                       offset: int = 0, limit: int = 100) -> dict:
        """Paged element listing for large sweeps (drill-down companion to
        array_status's buckets)."""
        counts = self.store.group_counts(group)
        if counts["total"] == 0:
            raise WeftError("task.invalid", f"unknown array group: {group}",
                            stage="infra")
        rows = self.store.jobs_in_group(group, state=state,
                                        offset=offset, limit=limit)
        return {"group": group, "offset": offset, "limit": limit,
                "state_filter": state, "total": counts["total"],
                "elements": [
                    {"index": j["array_index"], "job_id": j["job_id"],
                     "state": j["state"]} for j in rows]}

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
            if "job_id" in r:
                self.store.mark_superseded(j["job_id"], r["job_id"])
            out.append({"index": j["array_index"], "superseded": j["job_id"],
                        **{k: r[k] for k in ("job_id", "site") if k in r}})
        self.store.audit_log(None, "array.retry", command=group,
                             why=f"{len(out)} elements")
        return {"group": group, "retried": out}

    def array_result(self, group: str) -> dict:
        counts = self.store.group_counts(group)
        if counts["total"] == 0:
            raise WeftError("task.invalid", f"unknown array group: {group}",
                            stage="infra")
        return {"group": group, **self.runner.group_rollup(group)}

    def _check_denied(self, actor_action: str, site: str, cmd: str,
                      why: str) -> None:
        if not why or not why.strip():
            raise WeftError(
                "task.invalid", f"{actor_action} requires a non-empty why",
                stage="infra",
                hints={"reason": "every diagnostic command is audited with "
                                 "its rationale"},
            )
        for pat in DENY_PATTERNS:
            if pat.search(cmd):
                self.store.audit_log(None, f"{actor_action}.DENIED",
                                     site=site, command=cmd, why=why)
                raise WeftError(
                    "task.invalid",
                    "command matches the deny list; ask the user to run it "
                    "manually",
                    stage="infra", hints={"pattern": pat.pattern},
                )

    def job_node_exec(self, job_id: str, cmd: str, why: str,
                      timeout: float = 60.0) -> dict:
        """Run a diagnostic INSIDE a running job's allocation — live
        nvidia-smi/ps/df on the node MY job occupies (the login→node hop
        as a verb). Audited and deny-listed like site_exec. On non-
        scheduler sites the job runs on the host itself: the command runs
        in the job's directory instead."""
        job = self.store.get_job(job_id)
        if not job:
            raise WeftError("task.invalid", f"unknown job: {job_id}",
                            stage="infra")
        if job["state"] != "RUNNING":
            raise WeftError(
                "task.invalid",
                f"job {job_id} is {job['state']}, not RUNNING",
                stage="infra",
                hints={"suggestion": "node access rides the job's "
                                     "allocation — it exists only while "
                                     "the job runs; check task_logs for "
                                     "finished jobs"})
        site = job["site"]
        self._check_denied("job.node_exec", site, cmd, why)
        adapter = self._adapter(site)
        handle = job.get("sched_handle") or ""
        if hasattr(adapter, "node_exec") and handle.startswith("slurm:"):
            r = adapter.node_exec(handle, cmd, timeout=timeout)
        else:
            r = adapter.run_cmd(
                f"cd {shlex.quote(adapter.path(f'jobs/{job_id}'))} "
                f"&& ( {cmd} )", timeout=timeout)
        self.store.audit_log(None, "job.node_exec", site=site,
                             command=cmd, why=why, result=f"rc={r.rc}")
        return {"job_id": job_id, "rc": r.rc,
                "stdout": r.out[-8000:], "stderr": r.err[-4000:]}

    def site_exec(self, name: str, cmd: str, why: str) -> dict:
        """Guarded diagnostic shell (doc 05 §5): audited, deny-listed, scoped."""
        self._check_denied("site.exec", name, cmd, why)
        adapter = self._adapter(name)
        scoped = f"cd {shlex.quote(adapter.root)} && ( {cmd} )"
        r = adapter.run_cmd(scoped, timeout=120)
        self.store.audit_log(None, "site.exec", site=name, command=cmd,
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
                entry = {"site": name, "ok": False, "error": str(e)[:200]}
                if getattr(adapter, "jump", None) and \
                        hasattr(adapter, "hop_check"):
                    # multi-hop site: say WHICH hop died, not just "down"
                    entry["hops"] = adapter.hop_check()
                    dead = next((h["hop"] for h in entry["hops"]
                                 if h["ok"] is False), None)
                    if dead:
                        entry["diagnosis"] = f"chain breaks at {dead}"
                    else:
                        # the outage ended between the shim probe and the
                        # hop walk — never hand back a self-contradictory
                        # payload without saying why
                        entry["diagnosis"] = (
                            "every hop answers NOW: transient outage "
                            "(likely recovered) — retry the operation")
                checks.append(entry)
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
        actions = self.runner.reconcile()
        # retains enqueued by a process that died mid-transfer resume
        # here (placement is idempotent — re-copy over partials)
        import json as _json
        # pins whose settlement was missed by a dead process: capture if
        # the target has since reached a terminal state
        for row in self.store.retained_where(state="pinned-pending"):
            t = row["target"]
            trow = self.store.get_job(t) or self.store.get_kernel(t) or {}
            if trow.get("state") in ("DONE", "FAILED", "CANCELLED",
                                     "stopped", "died"):
                self.retains.settle_pins(t)
                actions.append({"retain": t, "action": "settle-pin"})
        for row in self.store.retained_where(state="queued") + \
                self.store.retained_where(state="inflight"):
            sel = _json.loads(row.get("selection") or "{}")
            self.run_retain(row["target"], include=sel.get("include"),
                            exclude=sel.get("exclude"),
                            dest=sel.get("dest"),
                            label=row["label"] or "", background=True,
                            layout=sel.get("layout") or "target")
            actions.append({"retain": row["target"],
                            "action": "resume-retain"})
        return actions

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

    def env_evict(self, env_id: str, site: str, archive: bool = False,
                  cascade: bool = False) -> dict:
        """Reclaim a realized environment's disk (GBs) while keeping the
        ability to come back. Default: drop the prefix — the site's shared
        package cache stays warm, so re-materialization is seconds and needs
        no network. archive=True additionally packs the env and keeps the
        blob on the CONTROLLER (reclaims ~100% of site space and rebuilds
        with no site network — the air-gapped path). Refuses if overlay envs
        stack on this prefix (cascade=True evicts them too). Distinct from
        env_repair: this is reclaiming, not fixing."""
        from . import evict as _evict
        return _evict.evict(self, env_id, site, archive=archive,
                            cascade=cascade)

    def gc_packages(self, site: str, confirm: bool = False) -> dict:
        """Clear the site's SHARED package cache. Consequential: after this,
        rebuilding an evicted env needs the index (or an archive). With the
        cache warm, rebuilds are seconds and offline."""
        from . import evict as _evict
        return _evict.gc_packages(self, site, confirm=confirm)

    def site_footprint(self, site: str) -> dict:
        """What occupies the site: prefixes vs shared package cache vs data
        cache, plus per-realization bytes and idle days — the numbers a host
        GC policy needs."""
        from . import evict as _evict
        return _evict.footprint(self, site)

    def gc_orphans(self, site: str, confirm: bool = False) -> dict:
        """Remove directories no record claims (crashed session clones, stale
        kernel sandboxes). Free bytes nothing else can reclaim."""
        from . import evict as _evict
        return _evict.gc_orphans(self, site, confirm=confirm)

    def gc_events(self, older_than_days: float = 30) -> dict:
        """Prune old events (terminal digests and failures are kept)."""
        pruned = self.store.prune_events(older_than_days)
        self.store.audit_log(None, "gc.events", result=f"pruned={pruned}")
        return {"pruned": pruned, "remaining": self.store.events_count()}

    # -- provenance -------------------------------------------------------------

    def bundle_export(self, job_id: str, out_path: str,
                      metadata=None) -> dict:
        """One file that re-derives a result anywhere: the finished job's
        provenance closure — task, env identities (specs + locks), every
        input blob (recursing through producing jobs), recorded outputs —
        as a tarball. The honest limits ride in `reproducibility`.
        `metadata`: an opaque caller-owned envelope (bytes or JSON) the
        bundle carries and bundle_import returns verbatim — never parsed,
        never part of identity or the re-derivation proof."""
        from .bundle import export_bundle
        return export_bundle(self, job_id, out_path, metadata=metadata)

    def bundle_import(self, path: str) -> dict:
        """Load a bundle into THIS workspace: envs, specs, input blobs.
        Returns the target task ready to task_submit (force=True) — equal
        output refs prove the re-derivation. `metadata` carries the
        exporter's sealed envelope verbatim (None if none was supplied)."""
        from .bundle import import_bundle
        return import_bundle(self, path)

    def run_inventory(self, target: str, glob: str | None = None,
                      min_bytes: int = 0,
                      max_entries: int = 5000) -> dict:
        """What a finished run LEFT BEHIND (recorded at terminal state,
        stat-only, budgeted): {path, bytes, mtime} per file — the
        triage facts for retention. Knowledge, not holdings: survives
        sandbox sweeps, run_retain and run_forget. Filters apply at
        read; `truncated` says the recording hit its budget."""
        row = self.store.get_run_inventory(target)
        if not row:
            raise WeftError(
                "data.missing", f"no inventory recorded for {target}",
                stage="infra",
                hints={"note": "inventories are recorded when a run "
                               "reaches a terminal state; runs finished "
                               "before this weft version have none"})
        entries = row["entries"]
        if glob:
            import fnmatch
            entries = [e for e in entries
                       if fnmatch.fnmatch(e["path"], glob)]
        if min_bytes:
            entries = [e for e in entries if e["bytes"] >= min_bytes]
        return {"target": target, "site": row["site"],
                "recorded_at": row["recorded_at"],
                "entries": entries[:max_entries],
                "matched": len(entries),
                "truncated": row["truncated"] or len(entries) > max_entries,
                "total_files": row["total"]}

    def run_retain(self, target: str, include: list[str] | None = None,
                   exclude: list[str] | None = None,
                   dest: str | None = None, max_gb: float | None = None,
                   label: str = "", background: bool = True,
                   layout: str = "target") -> dict:
        """Keep chosen files from a run as PLAIN FILES — in
        <workspace>/runs/<target>/ (background transfer for remote
        sites), or in place under the site's declared retain.dir.
        Placement per file: reflink → hardlink → copy → transfer,
        reported honestly. On a LIVE run, selections beyond completed
        blocks' artifact dirs become a PIN ("pinned-pending"): recorded
        now, captured when the run settles — the user usually means the
        eventual complete file, never a torn snapshot. One sidecar
        (.weft-run.json) carries run-level provenance. `label` groups
        several targets into one host-side unit; layout="label" nests
        the retained tree as runs/<label>/<target>/ so it mirrors the
        host's own run structure."""
        return self.retains.retain(target, include, exclude, dest, max_gb,
                                   label, background, layout=layout)

    def run_file_stat(self, target: str, rel: str) -> dict:
        """Existence + size + mtime of a file in a run's sandbox — the
        in-sandbox vs swept distinction a Files panel needs (inventory
        says what EXISTED; this says what's still on disk)."""
        return self.retains.file_stat(target, rel)

    def run_file_read(self, target: str, rel: str,
                      max_bytes: int = 1 << 20) -> dict:
        """Size-capped preview read from a run's sandbox (live or dead;
        path confined to the jobdir). Returns base64 bytes + truncated
        flag. A preview channel, not a transport — hard-capped at 8 MB;
        big files travel via data_register(path, site=…) → data_fetch,
        which also mints the lineage edge."""
        return self.retains.file_read(target, rel, max_bytes)

    def run_discard(self, target: str) -> dict:
        """Active sandbox GC: delete a finished run's sandbox NOW.
        Retained files and the terminal inventory survive. The passive
        default is policy run_remains_days via gc_plan/gc_sweep."""
        return self.retains.discard(target)

    def run_forget(self, target: str | None = None,
                   label: str | None = None) -> dict:
        """Reclaim the RETAINED tier: delete retained bytes wherever
        they live, drop the index on confirmed deletion (unreachable
        sites park forget_pending; retry later). Idempotent; by-label
        returns an itemized receipt. The terminal inventory always
        survives — holdings die here, knowledge never does."""
        return self.retains.forget(target, label)

    def retained_runs(self, label: str | None = None,
                      site: str | None = None) -> list[dict]:
        """Every retained run and where its bytes live — one query, no
        memory: {target, site, label, location, in_place, files, bytes,
        method, state, retained_at}."""
        return self.store.retained_where(label=label, site=site)

    def provenance(self, target: str, depth: int = 5) -> dict:
        """The full "how was this produced" chain for a job or a DataRef:
        command + exact env identity (spec, locked layers, snapshot dates,
        pinned SHAs, attested modules) + input refs, recursing into the
        jobs that produced those inputs — plus `placement`, WHERE it ran
        (site, node, allocation, partition, probe-derived node_truth),
        kept distinct from that node-agnostic closure. Everything needed
        for a methods appendix, machine-readable."""
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
            elif origin.startswith("run:") and depth > 0:
                # a RETAINED file re-entering compute: the chain walks
                # THROUGH it into the producing run (retention.md R5)
                run_target = origin[4:].split("/", 1)[0]
                if self.store.get_job(run_target):
                    node["produced_by"] = self.provenance(
                        run_target, depth - 1)
                else:
                    k = self.store.get_kernel(run_target)
                    if k:
                        node["produced_by"] = {
                            "kernel_id": run_target, "site": k["site"],
                            "state": k["state"],
                            "note": "interactive kernel run — "
                                    "kernel_transcript(kernel_id) is the "
                                    "derivation record"}
            return node

        job = self.store.get_job(target)
        if not job:
            raise WeftError("task.invalid", f"unknown job: {target}",
                            stage="infra")
        task = job["task"]
        m = job["manifest"] or {}
        node = {
            "schema": "provenance:v1",
            # no manifest (job failed or still running) = no claim — never
            # default to the BEST grade
            "reproducibility": m.get("reproducibility", "unknown"),
            "reproducibility_meaning": m.get("reproducibility_meaning"),
            "reproducibility_components": m.get("reproducibility_components"),
            "job_id": target, "state": job["state"], "site": job["site"],
            "task_hash": job["task_hash"],
            "command": task.get("command"),
            "env_vars": task.get("env_vars") or {},
            "outputs": [{"path": o["path"], "ref": o["ref"]}
                        for o in (job["manifest"] or {}).get("outputs", [])],
        }
        # WHERE it ran — first-class facts, deliberately distinct from the
        # node-agnostic reproducibility closure above: placement is
        # circumstance, never identity (a rerun elsewhere memoizes the
        # same). node_truth is labeled with its source: probe-derived
        # partition facts, not per-job measurements.
        res = task.get("resources") or {}
        caps = (self.store.get_site(job["site"]) or {}) \
            .get("capabilities") or {}
        partition = res.get("partition") or None
        truth, truth_src = None, None
        for p in (caps.get("scheduler") or {}).get("partitions") or []:
            if partition and p.get("name") == partition and p.get("compute"):
                truth = p["compute"]
                truth_src = f"deep probe of partition {partition!r}"
                break
        if truth is None:
            from .capability import compute_view
            cv = compute_view(caps)
            if cv:
                truth, truth_src = cv, "site probe"
        node["placement"] = {
            "site": job["site"],
            "node": m.get("node"),
            "allocation_id": job.get("sched_handle"),
            "partition": partition,
            "ran_at": {"wall_s": m.get("wall_s"),
                       "collected_at": job.get("updated_at")},
            "node_truth": {
                "glibc": truth.get("glibc"),
                "gpus": truth.get("gpus"),
                "cuda_driver": truth.get("cuda_driver"),
                "source": truth_src,
            } if truth else None,
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
                    # the agent's own rationale for adaptive steps —
                    # identity-neutral, so annotating never forks the EnvID
                    "notes": (spec or {}).get("notes") or [],
                    "step_notes": (spec or {}).get("step_notes") or {},
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

    def site_unregister(self, name: str) -> dict:
        """Forget a site's registration. Nothing site-side is touched
        (contrast site_teardown, which terminates cloud instances): the
        weft root, realized envs, and staged bytes stay on its disk —
        re-registering re-adopts them. Refuses while jobs, kernels, or
        services are live there. Data locations recorded at the site are
        forgotten; refs whose ONLY copy lived there will need the site
        re-registered (or a re-fetch from origin) to be staged again."""
        if not self.store.get_site(name):
            raise WeftError("task.invalid", f"unknown site: {name}",
                            stage="infra",
                            hints={"registered": [s["name"] for s in
                                                  self.store.list_sites()]})
        live = {
            "jobs": [j["job_id"] for j in self.store.nonterminal_jobs()
                     if j["site"] == name],
            "kernels": [k["kernel_id"] for k in
                        self.store.list_kernels(state="running")
                        if k["site"] == name],
            "services": [s["service_id"] for s in
                         self.store.list_services(state="ready")
                         if s["site"] == name],
        }
        busy = {k: v for k, v in live.items() if v}
        if busy:
            raise WeftError(
                "state.conflict",
                f"site {name} has live work; stop it first", stage="infra",
                hints={**busy,
                       "suggestion": "task_cancel / kernel_stop / "
                                     "service_stop the listed ids, or wait"})
        adapter = self.adapters.pop(name, None)
        if adapter is not None and hasattr(adapter, "close_control"):
            adapter.close_control()
        self.store.forget_site(name)
        self.store.audit_log("user", "site.unregister", site=name)
        self.store.emit("site.unregistered", site=name)
        return {"site": name, "state": "unregistered",
                "note": "site-side state untouched; re-registering "
                        "re-adopts realized envs and staged data"}

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
    "site_probe_deep", "site_load", "site_associations", "site_note",
    "site_route_probe", "module_check", "module_list", "site_exec",
    "job_node_exec", "site_teardown", "site_unregister",
    "env_ensure", "env_status", "env_why", "env_repair", "env_gpu_hint",
    "env_revise", "env_find_near",
    "env_publish", "env_adopt", "env_unpublish", "env_published",
    "data_register", "data_describe", "data_fetch",
    "task_submit", "task_status", "task_logs", "task_result", "task_cancel",
    "array_status", "array_elements", "array_result", "array_retry",
    "jobs_where", "list_envs", "list_kernels", "list_services", "audit_tail",
    "events_poll", "doctor", "reconcile", "provenance", "run_inventory",
    "run_retain", "retained_runs", "run_discard", "run_forget",
    "run_file_stat", "run_file_read",
    "bundle_export", "bundle_import",
    "gc_plan", "gc_sweep", "gc_events", "gc_packages", "gc_orphans",
    "env_evict", "site_footprint",
    "session_start", "session_exec", "session_install", "session_snapshot",
    "session_run_installer", "session_stop",
    "kernel_start", "kernel_exec", "kernel_poll", "kernel_peek",
    "kernel_status",
    "kernel_transcript", "kernel_interrupt", "kernel_restart", "kernel_stop",
    "kernel_promote",
    "service_start", "service_status", "service_stop",
]

for _name in PUBLIC_TOOLS:
    setattr(Weft, _name, tool(getattr(Weft, _name)))
