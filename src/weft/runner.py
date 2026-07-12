"""Job lifecycle engine (doc 01 §3-4).

PENDING -> RESOLVING_ENV -> STAGING -> [QUEUED] -> RUNNING -> COLLECTING
        -> DONE | FAILED(stage, code) | CANCELLED

Submission threads (bounded by `max_concurrent`) own a job only through
realization, staging, and submit; after that a per-site `SitePoller`
watches it with one batched query per tick, and a small collector pool
harvests results. Stages are idempotent or fenced, so a crashed controller
re-drives safely: realizations are marker-fenced, staging re-verifies, and
a job that already has a scheduler handle is reconciled from remote state,
never resubmitted. Collection that hits a site outage retries briefly and
then parks the job back with the poller — an outage of any length costs
waiting, never a wrong verdict.
"""

from __future__ import annotations

import shlex
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from .adapters.base import SiteAdapter
from .capability import satisfies_resources, scheduler_type
from .cas import LocalCAS
from .classify import classify_log
from .data import LOG_TAIL_LINES, DataManager
from .envman import EnvManager
from .errors import WeftError
from .placement import rank_sites
from .policy import enforce_policy, site_policy, storage_env_vars
from .poller import SitePoller, Watch
from .realize import ensure_realization
from .store import Store
from .task import Task

TERMINAL = ("DONE", "FAILED", "CANCELLED")
COLLECT_RETRIES = 4
COLLECT_BACKOFF_S = 3.0


class JobRunner:
    def __init__(
        self,
        store: Store,
        cas: LocalCAS,
        envman: EnvManager,
        dataman: DataManager,
        adapters: dict[str, SiteAdapter],
        transfers: dict,
        poll_interval: float = 0.5,
        max_concurrent: int = 8,
        collect_concurrency: int = 8,
        pixi_pack: str | None = None,
    ):
        self.store = store
        self.cas = cas
        self.envman = envman
        self.dataman = dataman
        self.adapters = adapters
        self.transfers = transfers
        self.poll_interval = poll_interval
        self.pixi_pack = pixi_pack
        self._slots = threading.Semaphore(max_concurrent)
        self._threads: dict[str, threading.Thread] = {}
        self._pollers: dict[str, SitePoller] = {}
        self._pollers_lock = threading.Lock()
        self._collectors = ThreadPoolExecutor(
            max_workers=collect_concurrency, thread_name_prefix="weft-collect"
        )
        self._collecting: set[str] = set()
        self._digest_lock = threading.Lock()
        self._last_digest: dict[str, dict] = {}
        self._done_digests: set[str] = set()
        self._load_cache: dict[str, tuple[float, dict]] = {}

    LOAD_TTL_S = 15.0

    def get_load(self, site: str, fresh: bool = False) -> dict | None:
        """Best-effort live-load view, TTL-cached (placement + site_load)."""
        adapter = self.adapters.get(site)
        if adapter is None or getattr(adapter, "launched", None) is False:
            return None  # never launch a cloud instance to measure its load
        now = time.time()
        cached = self._load_cache.get(site)
        if cached and not fresh and now - cached[0] < self.LOAD_TTL_S:
            return cached[1]
        try:
            info = adapter.load()
        except (WeftError, Exception):
            return None
        self._load_cache[site] = (now, info)
        return info

    def poller_for(self, site: str) -> SitePoller:
        with self._pollers_lock:
            p = self._pollers.get(site)
            if p is None:
                p = SitePoller(site, self.adapters[site], self)
                self._pollers[site] = p
            return p

    # -- submission ----------------------------------------------------------

    def submit(self, task: Task, *, force: bool = False, dry_run: bool = False,
               _group: str | None = None, _index: int | None = None) -> dict:
        if task.array:
            return self._submit_array(task, force=force, dry_run=dry_run)
        task_hash = task.task_hash()
        if not force:
            prior = self.store.latest_manifest_for_task(task_hash)
            if prior:
                return {"job_id": prior["job_id"], "memoized": True,
                        "manifest": prior,
                        "note": "identical task already completed; pass force=true to re-run"}

        site = self._place(task)
        plan = self._plan(task, site)
        if dry_run:
            return {"plan": plan, "site": site, "dry_run": True}

        job_id = "jb_" + uuid.uuid4().hex[:12]
        stored = task.to_dict()
        stored["site"] = site
        self.store.put_job(job_id, task_hash, stored, site, "PENDING",
                           array_group=_group, array_index=_index)
        self.store.emit("job.state", job_id=job_id, state="PENDING", site=site,
                        **self.group_payload(_group))
        t = threading.Thread(target=self._drive, args=(job_id,), daemon=True)
        self._threads[job_id] = t
        t.start()
        return {"job_id": job_id, "site": site, "plan": plan}

    def _submit_array(self, task: Task, *, force: bool, dry_run: bool) -> dict:
        group = "grp_" + uuid.uuid4().hex[:8]
        site = self._place(task)
        plan = self._plan(task, site)
        if dry_run:
            return {"plan": plan, "site": site, "elements": task.array, "dry_run": True}
        results = []
        for i in range(task.array):
            element = replace(
                task, array=None,
                env_vars={**task.env_vars, "WEFT_ARRAY_INDEX": str(i)},
            )
            r = self.submit(element, force=force, _group=group, _index=i)
            r["array_index"] = i
            results.append(r)
        job_ids = [r["job_id"] for r in results if "job_id" in r]
        return {"group": group, "site": site, "plan": plan,
                "elements": task.array, "jobs": results,
                "note": f"{len(job_ids)} element jobs submitted; watch "
                        f"array.progress events for the digest"}

    def _place(self, task: Task) -> str:
        if task.site != "auto":
            if task.site not in self.adapters:
                raise WeftError(
                    "task.invalid", f"unknown site: {task.site}", stage="submit",
                    hints={"registered": sorted(self.adapters)},
                )
            self._check_capabilities(task, task.site)
            return task.site
        modules = self.envman.extras(task.env)["modules"] if task.env else []
        refs = task.required_refs()
        sizes = self.dataman.sizes(refs)
        total = sum(sizes.values())
        realized = {
            r["site"]
            for r in (self.store.realizations_for(task.env) if task.env else [])
            if r["state"] == "ready"
        }
        present = {
            s["name"]: sum(sizes[r] for r in refs
                           if r in self.store.refs_present_at(s["name"]))
            for s in self.store.list_sites()
        }
        loads = {s["name"]: self.get_load(s["name"])
                 for s in self.store.list_sites()}
        result = rank_sites(
            task.resources.to_dict(), modules, self.store.list_sites(),
            realized, present, total, loads=loads,
        )
        if not result["ranked"]:
            raise WeftError(
                "env.unsatisfiable_on_site",
                "no registered site can run this task",
                stage="submit", hints={"rejected": result["rejected"]},
            )
        choice = result["ranked"][0]["site"]
        self._check_capabilities(task, choice)
        return choice

    def _check_capabilities(self, task: Task, site: str) -> None:
        row = self.store.get_site(site)
        policy = site_policy(row)
        active = len([j for j in self.store.nonterminal_jobs()
                      if j["site"] == site])
        enforce_policy(policy, task.resources.to_dict(), active, site)
        caps = (row or {}).get("capabilities")
        if not caps:
            return  # unprobed site: fail later with real signals, not guesses
        partitions = (caps.get("scheduler") or {}).get("partitions")
        allowed = policy.get("partitions_allowed")
        if partitions and allowed:
            partitions = [p for p in partitions if p["name"] in allowed]
        ok, hints = satisfies_resources(caps, task.resources.to_dict(),
                                        partitions=partitions)
        if not ok:
            raise WeftError(
                "site.capability_violation",
                f"resource ask exceeds what {site} offers",
                stage="submit",
                hints={**hints, "site": site,
                       "suggestion": "lower the ask to the reported max or pick another site"},
            )

    def _plan(self, task: Task, site: str) -> dict:
        plan: dict = {"site": site}
        if task.env:
            real = self.store.get_realization(task.env, site)
            plan["env"] = {
                "env_id": task.env,
                "action": "cached" if real and real["state"] == "ready" else "build",
            }
        else:
            plan["env"] = {"env_id": None, "action": "bare"}
        staging = self.dataman.plan_for(task.required_refs(), site)
        plan["staging"] = staging.to_dict()
        adapter = self.adapters.get(site)
        if staging.bytes_to_move and adapter is not None:
            plan["staging"].update(self._transfer_estimate(adapter, staging))
        row = self.store.get_site(site)
        caps = (row or {}).get("capabilities") or {}
        plan["queue"] = "none (interactive)" if scheduler_type(caps) == "none" \
            else scheduler_type(caps)
        notes = site_policy(row).get("notes")
        if notes:
            plan["site_policy_notes"] = notes  # user guidance — respect it
        return plan

    def _transfer_estimate(self, adapter, staging) -> dict:
        # never touch transfer_endpoint() on an unlaunched cloud site: the
        # plan must not cost money
        if getattr(adapter, "launched", None) is False:
            return {"transfer_method": "(determined after instance launch)"}
        try:
            endpoint = adapter.transfer_endpoint()
            method = self.transfers.get(endpoint["method"])
            if method is None:
                return {}
            est = method.estimate([("", staging.bytes_to_move)], endpoint)
            return {"transfer_method": endpoint["method"],
                    "estimate_s": est.get("seconds_guess")}
        except WeftError:
            return {}

    # -- lifecycle helpers (shared with the poller/collectors) ---------------

    @staticmethod
    def group_payload(group: str | None) -> dict:
        return {"array_group": group} if group else {}

    def set_job_state(self, job_id: str, state: str, **payload) -> None:
        self.store.update_job(job_id, state=state)
        self.store.emit("job.state", job_id=job_id, state=state, **payload)

    def tail_log(self, adapter: SiteAdapter, jobdir_rel: str,
                 lines: int = LOG_TAIL_LINES) -> str:
        try:
            r = adapter.shim(
                ["tail", "--file", adapter.path(f"{jobdir_rel}/log"),
                 "--lines", str(lines)], timeout=60,
            )
            return r.out if r.rc == 0 else ""
        except WeftError:
            return ""

    def _cancelled(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        return job is not None and job["state"] == "CANCELLED"

    # -- drive phase (realize + stage + submit; slot released after) ----------

    def _drive(self, job_id: str) -> None:
        with self._slots:
            try:
                self._drive_inner(job_id)
            except WeftError as e:
                if not self._cancelled(job_id):
                    self.store.update_job(job_id, state="FAILED", error=e.to_dict())
                    self._emit_failed(job_id, e)
            except Exception as e:  # infra bug — still a structured event
                err = WeftError("state.conflict", f"internal error: {e!r}", stage="infra")
                self.store.update_job(job_id, state="FAILED", error=err.to_dict())
                self._emit_failed(job_id, err)

    def _emit_failed(self, job_id: str, err: WeftError) -> None:
        job = self.store.get_job(job_id)
        group = job.get("array_group") if job else None
        self.store.emit("job.failed", job_id=job_id,
                        **self.group_payload(group), **err.to_dict())
        if group:
            self.emit_group_digest(group)

    def _drive_inner(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        task = Task.from_dict(job["task"])
        adapter = self.adapters[job["site"]]
        group = job.get("array_group")

        self.set_job_state(job_id, "RESOLVING_ENV", **self.group_payload(group))
        activate_line = "true"
        spec_env_vars: dict[str, str] = {}
        if task.env:
            env_row = self.store.get_env(task.env)
            if not env_row:
                raise WeftError("task.invalid", f"unknown EnvID {task.env}", stage="realize")
            extras = env_row["canonical"]["extras"]
            spec_env_vars = extras.get("env_vars") or {}
            site_row = self.store.get_site(job["site"]) or {}
            real = ensure_realization(
                task.env, env_row, adapter, self.store,
                caps=site_row.get("capabilities"),
                site_config=site_row.get("config"),
                pack_tools={"pixi_pack": self.pixi_pack, "cas": self.cas,
                            "transfers": self.transfers,
                            "solvers": self.envman.solvers},
            )
            activate_line = f". {shlex.quote(adapter.path(real['location']))}/activate.sh"

        if self._cancelled(job_id):
            return
        self.set_job_state(job_id, "STAGING", **self.group_payload(group))
        refs = task.required_refs()
        staged = self.dataman.ensure_at(refs, adapter, self.transfers,
                                        job_id=job_id)
        jobdir_rel = f"jobs/{job_id}"
        self._prepare_sandbox(adapter, jobdir_rel, task, job_id, activate_line,
                              spec_env_vars)
        self.store.emit("job.staged", job_id=job_id,
                        **self.group_payload(group), **staged)

        if self._cancelled(job_id):
            return
        handle = adapter.submit(jobdir_rel, task.to_dict())
        self.store.update_job(job_id, sched_handle=handle)
        scheduler = scheduler_type(
            (self.store.get_site(job["site"]) or {}).get("capabilities") or {}
        ) != "none"
        first = "QUEUED" if scheduler else "RUNNING"
        self.set_job_state(job_id, first, handle=handle,
                           **self.group_payload(group))
        self.poller_for(job["site"]).register(Watch(
            job_id=job_id, handle=handle, jobdir_rel=jobdir_rel, task=task,
            started_at=time.time(), scheduler=scheduler,
            array_group=group, last_state=first,
        ))

    def _prepare_sandbox(
        self, adapter: SiteAdapter, jobdir_rel: str, task: Task,
        job_id: str, activate_line: str, spec_env_vars: dict[str, str] | None = None,
    ) -> None:
        adapter.run_cmd(f"rm -rf {shlex.quote(adapter.path(jobdir_rel))}")
        plan_tsv = self.dataman.materialize_plan(task)
        adapter.write_file(f"{jobdir_rel}/activate.sh", (activate_line + "\n").encode())
        lines = [
            f"export WEFT_JOB_ID={shlex.quote(job_id)}",
            f"export WEFT_CPUS={task.resources.cpus}",
            f"export WEFT_MEM_GB={task.resources.mem_gb}",
            f"export WEFT_GPUS={task.resources.gpus}",
        ]
        for var, val in storage_env_vars(
            site_policy(self.store.get_site(adapter.name))
        ).items():
            lines.append(f"export {var}={shlex.quote(val)}")
        # env-spec vars first, task vars override (both support {{templates}})
        for k, v in {**(spec_env_vars or {}), **task.env_vars}.items():
            v = v.replace("{{cpus}}", str(task.resources.cpus))
            v = v.replace("{{mem_gb}}", str(task.resources.mem_gb))
            v = v.replace("{{gpus}}", str(task.resources.gpus))
            lines.append(f"export {k}={shlex.quote(v)}")
        lines.append("mkdir -p tmp")
        for out in task.outputs:
            d = out.rstrip("/") if out.endswith("/") else \
                (out.rsplit("/", 1)[0] if "/" in out else "")
            if d:
                lines.append(f"mkdir -p {shlex.quote(d)}")
        lines.append(task.command)
        adapter.write_file(f"{jobdir_rel}/cmd.sh", ("\n".join(lines) + "\n").encode())
        if plan_tsv:
            adapter.write_file(f"{jobdir_rel}/inputs.tsv", plan_tsv.encode())
            endpoint = adapter.transfer_endpoint()
            r = adapter.shim(
                ["materialize", "--cas", endpoint["cas_root"],
                 "--dir", adapter.path(jobdir_rel),
                 "--plan", adapter.path(f"{jobdir_rel}/inputs.tsv")],
                timeout=600,
            )
            if r.rc != 0:
                # blobs the location table promised are gone (purged scratch):
                # demote and let the caller re-drive
                for ref in task.required_refs():
                    self.store.demote_location(ref, adapter.name)
                raise WeftError(
                    "data.verify_failed",
                    f"sandbox materialization failed on {adapter.name}; "
                    "cached locations demoted",
                    stage="staging", retryable=True,
                    hints={"detail": r.err[:300],
                           "suggestion": "resubmit — staging will re-transfer"},
                )

    # -- collection (bounded pool; outage-tolerant) ----------------------------

    def enqueue_collect(self, watch: Watch, status: dict) -> None:
        with self._digest_lock:
            if watch.job_id in self._collecting:
                return
            self._collecting.add(watch.job_id)
        self.set_job_state(watch.job_id, "COLLECTING",
                           **self.group_payload(watch.array_group))
        self._collectors.submit(self._collect_guarded, watch, status)

    def _collect_guarded(self, watch: Watch, status: dict) -> None:
        adapter = self.adapters[watch.task.site] if watch.task.site in self.adapters \
            else self.adapters[self.store.get_job(watch.job_id)["site"]]
        try:
            backoff = COLLECT_BACKOFF_S
            for attempt in range(COLLECT_RETRIES + 1):
                try:
                    self._collect(watch.job_id, adapter, watch.jobdir_rel,
                                  watch.task, status, watch.array_group)
                    return
                except WeftError as e:
                    if e.code == "site.unreachable" and attempt < COLLECT_RETRIES:
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 30.0)
                        continue
                    if e.code == "site.unreachable":
                        # site is gone for now: park the job back with the
                        # poller — when the site returns, the exited status
                        # re-triggers collection; if the remote lost the
                        # files, two strikes turn it into node_failure
                        watch.lost_strikes = 0
                        self.poller_for(adapter.name).register(watch)
                        self.store.emit("collect.deferred", job_id=watch.job_id,
                                        site=adapter.name)
                        return
                    if not self._cancelled(watch.job_id):
                        self.store.update_job(watch.job_id, state="FAILED",
                                              error=e.to_dict())
                        self.store.emit("job.failed", job_id=watch.job_id,
                                        **self.group_payload(watch.array_group),
                                        **e.to_dict())
                    return
        except Exception as e:
            err = WeftError("state.conflict",
                            f"internal collection error: {e!r}", stage="collecting")
            self.store.update_job(watch.job_id, state="FAILED", error=err.to_dict())
            self.store.emit("job.failed", job_id=watch.job_id, **err.to_dict())
        finally:
            with self._digest_lock:
                self._collecting.discard(watch.job_id)
            if watch.array_group:
                self.emit_group_digest(watch.array_group)

    def _collect(
        self, job_id: str, adapter: SiteAdapter, jobdir_rel: str,
        task: Task, status: dict, group: str | None = None,
    ) -> None:
        exit_code = int(status.get("exit_code", -1))
        tail = self.tail_log(adapter, jobdir_rel)
        max_rss_gb = round(int(status.get("max_rss_kb", 0) or 0) / 1048576, 3)
        wall_s = float(status.get("wall_s", 0))

        if exit_code != 0:
            sig = classify_log(tail)
            mem_ask = task.resources.mem_gb
            if sig["signature"] == "oom-killed" or (
                exit_code == 137 and mem_ask and max_rss_gb >= 0.9 * mem_ask
            ):
                raise WeftError(
                    "job.oom", "job was killed for memory", stage="running",
                    hints={"observed_peak_gb": max_rss_gb, "requested_gb": mem_ask,
                           "log_signature": sig,
                           "suggestion": "resubmit with a larger mem_gb ask"},
                )
            raise WeftError(
                "job.nonzero_exit", f"command exited {exit_code}", stage="running",
                hints={"exit_code": exit_code, "log_signature": sig,
                       "log_tail": tail[-2000:], "jobdir": adapter.path(jobdir_rel)},
            )

        entries, total_bytes = self.dataman.collect_outputs(adapter, jobdir_rel, task)
        job = self.store.get_job(job_id)
        manifest = {
            "job_id": job_id,
            "task_hash": job["task_hash"],
            "env_id": task.env,
            "site": adapter.name,
            "exit_code": exit_code,
            "wall_s": wall_s,
            "max_rss_gb": max_rss_gb,
            "outputs": entries,
            "output_bytes": total_bytes,
            "logs": {"tail": "\n".join(tail.splitlines()[-LOG_TAIL_LINES:]),
                     "site_path": adapter.path(f"{jobdir_rel}/log")},
        }
        self.store.update_job(job_id, state="DONE", manifest=manifest)
        self.store.emit("job.done", job_id=job_id, site=adapter.name,
                        exit_code=exit_code, wall_s=wall_s,
                        outputs=len(entries), output_bytes=total_bytes,
                        **self.group_payload(group))

    # -- array digests -----------------------------------------------------------

    def emit_group_digest(self, group: str) -> None:
        """One coalesced digest per change, never per element (doc 05 §2)."""
        counts = self.store.group_counts(group)
        with self._digest_lock:
            if self._last_digest.get(group) == counts:
                return
            self._last_digest[group] = counts
            terminal = counts["total"] > 0 and (
                counts["done"] + counts["failed"] + counts["cancelled"]
                == counts["total"]
            )
            emit_done = terminal and group not in self._done_digests
            if emit_done:
                self._done_digests.add(group)
        payload = dict(counts)
        if counts["failed"]:
            payload["failed_previews"] = self.store.failed_in_group(group)
        self.store.emit("array.progress", array_group=group, **payload)
        if emit_done:
            self.store.emit("array.done", array_group=group,
                            **self.group_rollup(group))

    def group_rollup(self, group: str) -> dict:
        jobs = self.store.jobs_in_group(group)
        walls = sorted(j["manifest"]["wall_s"] for j in jobs
                       if j["manifest"] and "wall_s" in j["manifest"])
        rollup = {
            **self.store.group_counts(group),
            "failures": self.store.failed_in_group(group, limit=10),
            "output_bytes": sum(j["manifest"].get("output_bytes", 0)
                                for j in jobs if j["manifest"]),
            # string keys: this dict crosses JSON boundaries in events
            "elements": {str(j["array_index"]): {"job_id": j["job_id"],
                                                 "state": j["state"]}
                         for j in jobs},
        }
        if walls:
            rollup["wall_s"] = {"min": walls[0], "max": walls[-1],
                                "median": walls[len(walls) // 2]}
        return rollup

    # -- cancel / reconcile ----------------------------------------------------

    def cancel(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        if not job:
            raise WeftError("task.invalid", f"unknown job: {job_id}", stage="infra")
        if job["state"] in TERMINAL:
            return {"job_id": job_id, "state": job["state"], "note": "already terminal"}
        self.store.update_job(job_id, state="CANCELLED")
        self.store.emit("job.state", job_id=job_id, state="CANCELLED",
                        **self.group_payload(job.get("array_group")))
        if job["sched_handle"]:
            self.adapters[job["site"]].cancel(job["sched_handle"], f"jobs/{job_id}")
            self.poller_for(job["site"]).notify_cancel(job_id)
        return {"job_id": job_id, "state": "CANCELLED"}

    def reconcile(self) -> list[dict]:
        """Crash recovery: remote state is the source of truth (doc 01 §6).

        Deliberately does NOT poll inline — an unreachable site at restart
        must not fail reconciliation. Jobs with a handle are handed to the
        site poller, whose first tick classifies them (running -> resume,
        exited -> collect, gone -> two-strike node_failure) and whose
        outage handling covers a site that is down right now.
        """
        actions = []
        for job in self.store.nonterminal_jobs():
            job_id = job["job_id"]
            if job_id in self._threads and self._threads[job_id].is_alive():
                continue
            with self._digest_lock:
                if job_id in self._collecting:
                    continue
            adapter = self.adapters.get(job["site"])
            if adapter is None:
                continue
            if self.poller_for(job["site"]).watching(job_id):
                continue
            task = Task.from_dict(job["task"])
            if job["sched_handle"]:
                scheduler = scheduler_type(
                    (self.store.get_site(job["site"]) or {}).get("capabilities")
                    or {}) != "none"
                self.poller_for(job["site"]).register(Watch(
                    job_id=job_id, handle=job["sched_handle"],
                    jobdir_rel=f"jobs/{job_id}", task=task,
                    started_at=job["created_at"], scheduler=scheduler,
                    array_group=job.get("array_group"),
                    last_state=job["state"],
                ))
                actions.append({"job": job_id, "action": "resume-poll"})
            else:
                # never reached submission: stages are idempotent, re-drive
                actions.append({"job": job_id, "action": "re-drive"})
                t = threading.Thread(target=self._drive, args=(job_id,), daemon=True)
                self._threads[job_id] = t
                t.start()
        return actions

    def wait(self, job_id: str, timeout: float = 300.0) -> dict:
        """Testing/synchronous helper — the agent API never blocks like this."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.store.get_job(job_id)
            if job and job["state"] in TERMINAL:
                return job
            time.sleep(0.05)
        raise TimeoutError(f"job {job_id} not terminal after {timeout}s")
