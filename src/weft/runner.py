"""Job lifecycle engine (doc 01 §3-4).

PENDING -> RESOLVING_ENV -> STAGING -> [QUEUED] -> RUNNING -> COLLECTING
        -> DONE | FAILED(stage, code) | CANCELLED

Stages are idempotent or fenced, so a crashed controller re-drives safely:
realizations are marker-fenced, staging re-verifies, and a job that already
has a scheduler handle is reconciled from remote state, never resubmitted.
"""

from __future__ import annotations

import shlex
import threading
import time
import uuid
from dataclasses import replace

from .adapters.base import SiteAdapter
from .capability import satisfies_resources, scheduler_type
from .cas import LocalCAS
from .classify import classify_log
from .data import LOG_TAIL_LINES, DataManager
from .envman import EnvManager
from .errors import WeftError
from .placement import rank_sites
from .realize import ensure_prefix_realization, env_dir_rel
from .store import Store
from .task import Task

TERMINAL = ("DONE", "FAILED", "CANCELLED")
WALLTIME_GRACE_S = 10.0


def parse_walltime(w: str) -> float | None:
    if not w:
        return None
    parts = [int(p) for p in w.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3:]
    return h * 3600 + m * 60 + s


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
    ):
        self.store = store
        self.cas = cas
        self.envman = envman
        self.dataman = dataman
        self.adapters = adapters
        self.transfers = transfers
        self.poll_interval = poll_interval
        self._slots = threading.Semaphore(max_concurrent)
        self._threads: dict[str, threading.Thread] = {}

    # -- submission ----------------------------------------------------------

    def submit(self, task: Task, *, force: bool = False, dry_run: bool = False) -> dict:
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
        self.store.put_job(job_id, task_hash, stored, site, "PENDING")
        self.store.emit("job.state", job_id=job_id, state="PENDING", site=site)
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
            r = self.submit(element, force=force)
            r["array_index"] = i
            results.append(r)
        job_ids = [r["job_id"] for r in results if "job_id" in r]
        return {"group": group, "site": site, "plan": plan,
                "elements": task.array, "jobs": results,
                "note": f"{len(job_ids)} element jobs submitted"}

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
        result = rank_sites(
            task.resources.to_dict(), modules, self.store.list_sites(),
            realized, present, total,
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
        caps = (row or {}).get("capabilities")
        if not caps:
            return  # unprobed site: fail later with real signals, not guesses
        ok, hints = satisfies_resources(caps, task.resources.to_dict())
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
        row = self.store.get_site(site)
        caps = (row or {}).get("capabilities") or {}
        plan["queue"] = "none (interactive)" if scheduler_type(caps) == "none" \
            else scheduler_type(caps)
        return plan

    # -- lifecycle -----------------------------------------------------------

    def _set_state(self, job_id: str, state: str, **payload) -> None:
        self.store.update_job(job_id, state=state)
        self.store.emit("job.state", job_id=job_id, state=state, **payload)

    def _cancelled(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        return job is not None and job["state"] == "CANCELLED"

    def _drive(self, job_id: str) -> None:
        with self._slots:
            try:
                self._drive_inner(job_id)
            except WeftError as e:
                if not self._cancelled(job_id):
                    self.store.update_job(job_id, state="FAILED", error=e.to_dict())
                    self.store.emit("job.failed", job_id=job_id, **e.to_dict())
            except Exception as e:  # infra bug — still a structured event
                err = WeftError("state.conflict", f"internal error: {e!r}", stage="infra")
                self.store.update_job(job_id, state="FAILED", error=err.to_dict())
                self.store.emit("job.failed", job_id=job_id, **err.to_dict())

    def _drive_inner(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        task = Task.from_dict(job["task"])
        adapter = self.adapters[job["site"]]

        self._set_state(job_id, "RESOLVING_ENV")
        activate_line = "true"
        spec_env_vars: dict[str, str] = {}
        if task.env:
            env_row = self.store.get_env(task.env)
            if not env_row:
                raise WeftError("task.invalid", f"unknown EnvID {task.env}", stage="realize")
            extras = env_row["canonical"]["extras"]
            spec_env_vars = extras.get("env_vars") or {}
            real = ensure_prefix_realization(
                task.env, env_row, adapter, self.store,
                modules=extras["modules"] or None,
            )
            activate_line = f". {shlex.quote(adapter.path(real['location']))}/activate.sh"

        if self._cancelled(job_id):
            return
        self._set_state(job_id, "STAGING")
        refs = task.required_refs()
        staged = self.dataman.ensure_at(refs, adapter, self.transfers)
        jobdir_rel = f"jobs/{job_id}"
        self._prepare_sandbox(adapter, jobdir_rel, task, job_id, activate_line,
                              spec_env_vars)
        self.store.emit("job.staged", job_id=job_id, **staged)

        if self._cancelled(job_id):
            return
        handle = adapter.submit(jobdir_rel, task.to_dict())
        self.store.update_job(job_id, sched_handle=handle)
        queued = scheduler_type(
            (self.store.get_site(job["site"]) or {}).get("capabilities") or {}
        ) != "none"
        self._set_state(job_id, "QUEUED" if queued else "RUNNING", handle=handle)
        self._monitor(job_id, adapter, handle, jobdir_rel, task)

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

    def _monitor(
        self, job_id: str, adapter: SiteAdapter, handle: str,
        jobdir_rel: str, task: Task,
    ) -> None:
        limit = parse_walltime(task.resources.walltime)
        started = time.time()
        scheduler = scheduler_type(
            (self.store.get_site(adapter.name) or {}).get("capabilities") or {}
        ) != "none"
        while True:
            status = adapter.poll_job(handle, jobdir_rel)
            state = status.get("state")
            if state == "exited":
                break
            if state in ("lost", "missing"):
                raise WeftError(
                    "sched.node_failure",
                    "job process disappeared without an exit record",
                    stage="running",
                    hints={"last_log": self._tail(adapter, jobdir_rel),
                           "jobdir": adapter.path(jobdir_rel)},
                )
            if state == "running":
                job = self.store.get_job(job_id)
                if job["state"] == "QUEUED":
                    self._set_state(job_id, "RUNNING")
                if job["state"] == "CANCELLED":
                    adapter.cancel(handle, jobdir_rel)
                    return
            # controller-side walltime for non-scheduler sites gives uniform
            # semantics everywhere; scheduler sites enforce their own
            if limit and not scheduler and time.time() - started > limit + WALLTIME_GRACE_S:
                adapter.cancel(handle, jobdir_rel)
                raise WeftError(
                    "job.walltime_exceeded",
                    f"exceeded requested walltime {task.resources.walltime}",
                    stage="running",
                    hints={"walltime_s": limit, "elapsed_s": time.time() - started,
                           "suggestion": "raise resources.walltime or shrink the task"},
                )
            time.sleep(self.poll_interval)

        if self._cancelled(job_id):
            return
        self._set_state(job_id, "COLLECTING")
        self._collect(job_id, adapter, jobdir_rel, task, status)

    def _tail(self, adapter: SiteAdapter, jobdir_rel: str, lines: int = LOG_TAIL_LINES) -> str:
        r = adapter.shim(
            ["tail", "--file", adapter.path(f"{jobdir_rel}/log"), "--lines", str(lines)],
            timeout=60,
        )
        return r.out if r.rc == 0 else ""

    def _collect(
        self, job_id: str, adapter: SiteAdapter, jobdir_rel: str,
        task: Task, status: dict,
    ) -> None:
        exit_code = int(status.get("exit_code", -1))
        tail = self._tail(adapter, jobdir_rel)
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
                        outputs=len(entries), output_bytes=total_bytes)

    # -- cancel / reconcile ----------------------------------------------------

    def cancel(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        if not job:
            raise WeftError("task.invalid", f"unknown job: {job_id}", stage="infra")
        if job["state"] in TERMINAL:
            return {"job_id": job_id, "state": job["state"], "note": "already terminal"}
        self.store.update_job(job_id, state="CANCELLED")
        self.store.emit("job.state", job_id=job_id, state="CANCELLED")
        if job["sched_handle"]:
            self.adapters[job["site"]].cancel(job["sched_handle"], f"jobs/{job_id}")
        return {"job_id": job_id, "state": "CANCELLED"}

    def reconcile(self) -> list[dict]:
        """Crash recovery: remote state is the source of truth (doc 01 §6)."""
        actions = []
        for job in self.store.nonterminal_jobs():
            job_id = job["job_id"]
            if job_id in self._threads and self._threads[job_id].is_alive():
                continue
            task = Task.from_dict(job["task"])
            adapter = self.adapters.get(job["site"])
            if adapter is None:
                continue
            if job["sched_handle"]:
                status = adapter.poll_job(job["sched_handle"], f"jobs/{job_id}")
                if status.get("state") == "exited":
                    actions.append({"job": job_id, "action": "collect-after-restart"})
                    t = threading.Thread(
                        target=self._finish_reconciled,
                        args=(job_id, adapter, task, status), daemon=True,
                    )
                    self._threads[job_id] = t
                    t.start()
                elif status.get("state") == "running":
                    actions.append({"job": job_id, "action": "resume-monitor"})
                    t = threading.Thread(
                        target=self._resume_monitor,
                        args=(job_id, adapter, job["sched_handle"], task), daemon=True,
                    )
                    self._threads[job_id] = t
                    t.start()
                else:
                    err = WeftError(
                        "sched.node_failure",
                        "controller restarted and the job left no exit record",
                        stage="running",
                        hints={"poll": status},
                    )
                    self.store.update_job(job_id, state="FAILED", error=err.to_dict())
                    self.store.emit("job.failed", job_id=job_id, **err.to_dict())
                    actions.append({"job": job_id, "action": "marked-lost"})
            else:
                # never reached submission: stages are idempotent, re-drive
                actions.append({"job": job_id, "action": "re-drive"})
                t = threading.Thread(target=self._drive, args=(job_id,), daemon=True)
                self._threads[job_id] = t
                t.start()
        return actions

    def _finish_reconciled(self, job_id, adapter, task, status) -> None:
        with self._slots:
            try:
                self._set_state(job_id, "COLLECTING")
                self._collect(job_id, adapter, f"jobs/{job_id}", task, status)
            except WeftError as e:
                self.store.update_job(job_id, state="FAILED", error=e.to_dict())
                self.store.emit("job.failed", job_id=job_id, **e.to_dict())

    def _resume_monitor(self, job_id, adapter, handle, task) -> None:
        with self._slots:
            try:
                self._monitor(job_id, adapter, handle, f"jobs/{job_id}", task)
            except WeftError as e:
                if not self._cancelled(job_id):
                    self.store.update_job(job_id, state="FAILED", error=e.to_dict())
                    self.store.emit("job.failed", job_id=job_id, **e.to_dict())

    def wait(self, job_id: str, timeout: float = 300.0) -> dict:
        """Testing/synchronous helper — the agent API never blocks like this."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.store.get_job(job_id)
            if job and job["state"] in TERMINAL:
                return job
            time.sleep(0.05)
        raise TimeoutError(f"job {job_id} not terminal after {timeout}s")
