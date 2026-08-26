"""Per-site polling service: one batched status query per site per tick.

Replaces per-job monitor threads. Thread count is bounded by the number of
*sites* with outstanding jobs (plus a small shared collector pool), not by
the number of jobs — the difference between 8 and 2000 in-flight elements.

Failure semantics, deliberately:
  * a transport failure is ONE site-level outage — one `site.unreachable`
    event, exponential backoff owned by the poller, jobs untouched (they
    are detached; remote state is the truth);
  * a job with no live process and no exit record needs two consecutive
    strikes before `sched.node_failure` — a single weird poll during an
    outage or startup proves nothing;
  * the poller thread never dies with jobs registered: a tick that throws
    emits `poller.error` and keeps going.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .errors import WeftError
from .runner_util import parse_walltime

WALLTIME_GRACE_S = 10.0
OUTAGE_BACKOFF_CAP_S = 30.0
LOST_STRIKES = 2
UNREACHABLE_REDRIVE_STRIKES = 3
IDLE_TICKS_BEFORE_EXIT = 5


@dataclass
class Watch:
    job_id: str
    handle: str
    jobdir_rel: str
    task: object                 # weft.task.Task
    started_at: float
    scheduler: bool              # scheduler sites enforce walltime themselves
    array_group: str | None = None
    last_state: str = ""         # last lifecycle state we recorded
    last_reason: str = ""        # last scheduler pending-reason recorded
    lost_strikes: int = 0
    cancelled: bool = False
    cancel_sent: bool = False
    lease: str | None = None     # "kernel"|"service": report deaths, not results
    unreachable_strikes: int = 0  # probe ticks that rode dead transport
    probe: bool = False          # deferred submit (site outage cut _drive):
                                 # on the next successful poll, JOBDIR TRUTH
                                 # decides — exited -> collect, running ->
                                 # adopt, absent -> re-drive once
    deferred_since: float = 0.0  # grace anchor for probe watches


class SitePoller:
    def __init__(self, site: str, adapter, runner):
        self.site = site
        self.adapter = adapter
        self.runner = runner
        self._watches: dict[str, Watch] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._stopped = False
        self._outage_since: float | None = None
        self._backoff = 0.0

    # -- registration -------------------------------------------------------

    def register(self, watch: Watch) -> None:
        with self._lock:
            self._watches[watch.job_id] = watch
            self._ensure_thread()
        self._wake.set()

    def notify_cancel(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._watches:
                self._watches[job_id].cancelled = True
        self._wake.set()

    def watching(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._watches

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._run, daemon=True, name=f"weft-poll-{self.site}"
            )
            self._thread.start()

    def _interval(self) -> float:
        row = self.runner.store.get_site(self.site) or {}
        policy = (row.get("config") or {}).get("policy") or {}
        return float(policy.get("poll_interval_s")
                     or self.runner.poll_interval)

    def _requeue_grace(self) -> float:
        row = self.runner.store.get_site(self.site) or {}
        policy = (row.get("config") or {}).get("policy") or {}
        return float(policy.get("outage_requeue_grace_s") or 3600.0)

    # -- loop ------------------------------------------------------------------

    def stop(self) -> None:
        """Stop the poll loop (Weft.close): a poller with LIVE watches
        otherwise polls forever — ~50 never-closed instances in one
        test lane each opening ssh sessions every tick starved sshd's
        MaxSessions/MaxStartups and the deferred-probe re-drives with
        it (R1b). Watches are NOT failed — a reopened Weft re-attaches
        via resume."""
        self._stopped = True
        self._wake.set()

    def _run(self) -> None:
        idle = 0
        while not self._stopped:
            self._wake.wait(timeout=self._backoff or self._interval())
            self._wake.clear()
            if self._stopped:
                self._thread = None
                return
            with self._lock:
                items = list(self._watches.values())
                if not items:
                    idle += 1
                    if idle >= IDLE_TICKS_BEFORE_EXIT:
                        self._thread = None
                        return
                    continue
            idle = 0
            try:
                self._tick(items)
            except Exception as e:  # the poller must outlive any tick bug
                self.runner.store.emit("poller.error", site=self.site,
                                       detail=repr(e)[:300])
                time.sleep(self._interval())

    def _note_outage(self, jobs_waiting: int) -> None:
        # one outage, one event — regardless of how many jobs wait it out
        if self._outage_since is None:
            self._outage_since = time.time()
            self.runner.store.set_health(self.site, "unreachable")
            self.runner.store.emit("site.unreachable", site=self.site,
                                   jobs_waiting=jobs_waiting)
        self._backoff = min(max(self._backoff * 2, self._interval() * 2),
                            OUTAGE_BACKOFF_CAP_S)

    def _note_reachable(self) -> None:
        if self._outage_since is not None:
            self.runner.store.set_health(self.site, "ok")
            self.runner.store.emit(
                "site.reachable", site=self.site,
                outage_s=round(time.time() - self._outage_since, 1),
            )
            self._outage_since = None
        self._backoff = 0.0

    def _enforce_probe_grace(self, items: list[Watch]) -> set:
        """RUNNING jobs are detached and wait out any outage (remote
        state is the truth). Deferred SUBMITS are different: parked limbo
        must be bounded — past the grace the honest terminal verdict
        lands, with the deferral context and the lever ("unknown" must
        never quietly become "unlimited"). Store writes only — runs
        whether or not the site answers. Returns the failed job_ids."""
        grace = self._requeue_grace()
        dropped: set = set()
        for w in items:
            if w.probe and w.deferred_since \
                    and time.time() - w.deferred_since > grace:
                dropped.add(w.job_id)
                self._fail(w, WeftError(
                    "site.unreachable",
                    f"submit deferred by a site outage and {self.site} "
                    f"stayed unreachable past the grace window",
                    stage="submit", retryable=True,
                    hints={"deferred_for_s": round(
                               time.time() - w.deferred_since, 1),
                           "grace_s": grace,
                           "lever": "site policy outage_requeue_grace_s",
                           "suggestion": "resubmit when the site "
                                         "returns; memoization makes "
                                         "the retry cheap"}))
        return dropped

    def _tick(self, items: list[Watch]) -> None:
        dropped = self._enforce_probe_grace(items)
        if dropped:
            items = [w for w in items if w.job_id not in dropped]
        # probe watches self-serve jobdir truth via the shim — their fake
        # handles must not reach a scheduler's batch query (squeue on a
        # garbage id would error the whole batch into a phantom outage)
        polled = [w for w in items if not w.probe]
        statuses: dict[str, dict] = {}
        if polled:
            try:
                statuses = self.adapter.poll_jobs(
                    [(w.handle, w.jobdir_rel) for w in polled]
                )
            except WeftError as e:
                if e.code != "site.unreachable":
                    # a site-fatal error (e.g. budget.exceeded tore the
                    # cloud instance down) kills every job watched here —
                    # burying it as a poller.error would leave them
                    # RUNNING forever
                    for w in items:
                        self._fail(w, e)
                        if w.array_group:
                            self.runner.emit_group_digest(w.array_group)
                    return
                self._note_outage(len(items))
                return
            self._note_reachable()

        dirty_groups: set[str] = set()
        for w in items:
            was_probe = w.probe
            try:
                self._transition(w, statuses.get(w.handle, {"state": "unknown"}))
                if was_probe:
                    # its shim call answered: the site is reachable, even
                    # when this poller watches probes only
                    self._note_reachable()
            except WeftError as e:
                if e.code == "site.unreachable":
                    # an adapter call INSIDE the transition (cancel,
                    # tail_log, probe status) rode dead transport. Site-
                    # scoped, never this job's verdict: keep the watch,
                    # engage the outage machinery, skip the tick. (This
                    # path minted FAILED(site.unreachable) on the
                    # walltime-cancel path before — aba, live,
                    # 2026-08-09.)
                    if was_probe and self._probe_unreachable(w):
                        continue
                    self._note_outage(len(items))
                    continue
                self._fail(w, e)
            except Exception as e:
                import traceback
                self._fail(w, WeftError(
                    "internal.error", f"internal poller error: {e!r}",
                    stage="running",
                    hints={"traceback_tail": traceback.format_exc()[-1200:]},
                ))
            if w.array_group:
                dirty_groups.add(w.array_group)
        for group in dirty_groups:
            self.runner.emit_group_digest(group)

    # -- per-job transitions (the old monitor logic, verbatim semantics) -------

    def _unregister(self, job_id: str) -> None:
        with self._lock:
            self._watches.pop(job_id, None)

    def _transition(self, w: Watch, status: dict) -> None:
        if w.probe:
            # parked submit (site outage cut _drive): jobdir truth decides,
            # via the shim directly — batch poll status is keyed by a
            # handle this job never got (and slurm's batch poll is
            # scheduler-side only)
            self._probe_transition(w)
            return
        if w.cancelled:
            # CONFIRM before unregistering: scancel is fire-and-forget on
            # the scheduler side — a swallowed failure left the job
            # running on the cluster while weft said CANCELLED (2026-07
            # sweep S6). Watch until the scheduler agrees; resend + emit
            # if it still reports the job alive.
            if not w.cancel_sent:
                self.adapter.cancel(w.handle, w.jobdir_rel)
                w.cancel_sent = True
                return
            if status.get("state") in ("queued", "running"):
                self.adapter.cancel(w.handle, w.jobdir_rel)
                self.runner.store.emit(
                    "job.cancel_retry", job_id=w.job_id,
                    note="scheduler still reports the job after cancel; "
                         "resent")
                return
            self._unregister(w.job_id)
            return
        state = status.get("state")
        if w.lease:
            self._lease_transition(w, state, status)
            return

        if state == "exited":
            self._unregister(w.job_id)
            self.runner.enqueue_collect(w, status)
            return
        if state == "timeout":
            raise WeftError(
                "job.walltime_exceeded",
                "scheduler killed the job at its time limit",
                stage="running",
                hints={"requested": w.task.resources.walltime,
                       "slurm_state": status.get("slurm"),
                       "suggestion": "raise resources.walltime or shrink the task"},
            )
        if state == "oom":
            raise WeftError(
                "job.oom", "scheduler killed the job for memory",
                stage="running",
                hints={"requested_gb": w.task.resources.mem_gb,
                       "observed_peak_gb": round(
                           int(status.get("max_rss_kb", 0) or 0) / 1048576, 3),
                       "suggestion": "resubmit with mem_gb >= "
                                     "max(2 x requested, 1.5 x observed peak)",
                       "note": "observed peak UNDERSTATES need when the kill "
                               "happened during allocation — never size down "
                               "toward it"},
            )
        if state == "cancelled":
            self.runner.store.update_job(w.job_id, state="CANCELLED")
            self.runner.store.emit("job.state", job_id=w.job_id,
                                   state="CANCELLED", by="scheduler",
                                   **self.runner.group_payload(w.array_group))
            self._unregister(w.job_id)
            return
        if state in ("lost", "missing", "unknown"):
            w.lost_strikes += 1
            if w.lost_strikes >= LOST_STRIKES:
                raise WeftError(
                    "sched.node_failure",
                    "job process disappeared without an exit record "
                    "(remote crash or reboot?)",
                    stage="running",
                    hints={"jobdir": self.adapter.path(w.jobdir_rel),
                           "last_log": self.runner.tail_log(
                               self.adapter, w.jobdir_rel, 30)},
                )
            return
        w.lost_strikes = 0

        if state == "queued":
            reason = status.get("reason") or ""
            if reason and reason != w.last_reason:
                # why it pends (Priority/Resources/QOS…) names the workaround
                w.last_reason = reason
                self.runner.store.update_job(w.job_id, queue_reason=reason)
        if state == "running" and w.last_state == "QUEUED":
            w.last_state = "RUNNING"
            # measured queue wait: the raw material for honest ETAs
            self.runner.store.add_metric(
                self.site, "queue_wait_s",
                round(time.time() - w.started_at, 1))
            self.runner.set_job_state(w.job_id, "RUNNING",
                                      **self.runner.group_payload(w.array_group))

        # controller-side walltime on non-scheduler sites: uniform semantics
        limit = parse_walltime(w.task.resources.walltime)
        if (limit and not w.scheduler
                and time.time() - w.started_at > limit + WALLTIME_GRACE_S):
            self.adapter.cancel(w.handle, w.jobdir_rel)
            raise WeftError(
                "job.walltime_exceeded",
                f"exceeded requested walltime {w.task.resources.walltime}",
                stage="running",
                hints={"walltime_s": limit,
                       "elapsed_s": round(time.time() - w.started_at, 1),
                       "suggestion": "raise resources.walltime or shrink the task"},
            )

    def _probe_unreachable(self, w: Watch) -> bool:
        """A probe tick rode dead transport. When the parked drive NEVER
        reached its submit call (deferral.submit_attempted False — the
        submit is the one call whose loss can leave a live remote run),
        waiting for the site to ANSWER can deadlock: an ephemeral site
        (cloud instance, torn-down container) only exists when a drive
        provisions it, so answering needs the re-drive and the re-drive
        waited on answering — both R1b jobs sat STAGING against the
        3600s grace while their tests timed out. After N consecutive
        unreachable ticks, re-drive ONCE: the drive owns provisioning
        and either lands or produces a typed verdict; a second cut hits
        the existing attempts guard, and the grace window stays the
        outer bound. Returns True when the watch was handed back to the
        runner (caller skips the outage bookkeeping for this tick)."""
        w.unreachable_strikes += 1
        if w.unreachable_strikes < UNREACHABLE_REDRIVE_STRIKES:
            return False
        store = self.runner.store
        job = store.get_job(w.job_id)
        dfr = dict((job or {}).get("deferral") or {})
        if not job or dfr.get("submit_attempted") \
                or int(dfr.get("attempts") or 0) >= 1:
            return False
        dfr["attempts"] = int(dfr.get("attempts") or 0) + 1
        store.set_job_deferral(w.job_id, dfr)
        store.emit("job.redriven", job_id=w.job_id, site=self.site,
                   note="submit was never attempted and the site stayed "
                        "unreachable — re-driving (the drive owns "
                        "provisioning/reachability)")
        self._unregister(w.job_id)
        import threading as _th
        _th.Thread(target=self.runner._drive, args=(w.job_id,),
                   daemon=True).start()
        return True

    def _probe_transition(self, w: Watch) -> None:
        """A submit cut by a site outage was PARKED, not failed. Now the
        site answers again: decide from what actually happened on disk.
        exited -> collect (the run finished without us); running -> adopt
        the live pid and resume normal supervision; nothing there after
        two strikes -> the submit never delivered: re-drive ONCE, then
        the honest terminal verdict. Scheduler sites get a positive
        queue check by job name before the re-drive — an sbatch whose
        reply was lost may sit PENDING with an empty jobdir, and a blind
        re-drive would run the task twice."""
        store = self.runner.store
        job = store.get_job(w.job_id)
        if not job or job["state"] in ("DONE", "FAILED", "CANCELLED") \
                or w.cancelled:
            self._unregister(w.job_id)
            return
        st = self.adapter.shim(
            ["status", "--dir", self.adapter.path(w.jobdir_rel)],
            timeout=self.adapter.poll_timeout
            if hasattr(self.adapter, "poll_timeout") else 60.0).json()
        w.unreachable_strikes = 0   # the site answered — strikes are
        state = st.get("state")     # CONSECUTIVE dead-transport ticks
        if state == "exited":
            store.set_job_deferral(w.job_id, None)
            store.update_job(w.job_id, queue_reason="")
            store.emit("job.recovered", job_id=w.job_id, site=self.site,
                       found="exited",
                       note="deferred submit had delivered; the run "
                            "finished during the outage — collecting")
            self._unregister(w.job_id)
            w.probe = False
            self.runner.enqueue_collect(w, st)
            return
        if state == "running" and st.get("pid"):
            handle = f"pid:{st['pid']}"
            w.probe = False
            w.handle = handle
            w.last_state = "RUNNING"
            w.lost_strikes = 0
            store.set_job_deferral(w.job_id, None)
            store.update_job(w.job_id, sched_handle=handle, queue_reason="",
                             submitted_at=w.deferred_since or time.time())
            store.emit("job.recovered", job_id=w.job_id, site=self.site,
                       found="running", handle=handle)
            self.runner.set_job_state(
                w.job_id, "RUNNING", **self.runner.group_payload(w.array_group))
            return
        # nothing usable on disk — one weird poll proves nothing
        w.lost_strikes += 1
        if w.lost_strikes < LOST_STRIKES:
            return
        dfr = dict(job.get("deferral") or {})
        if w.scheduler:
            find = getattr(self.adapter, "find_handle_by_name", None)
            handle = find(f"weft-{w.job_id}") if find else None
            if handle:
                # the sbatch DID land; the job pends with an empty jobdir
                w.probe = False
                w.handle = handle
                w.last_state = "QUEUED"
                w.lost_strikes = 0
                store.set_job_deferral(w.job_id, None)
                store.update_job(w.job_id, sched_handle=handle,
                                 queue_reason="")
                store.emit("job.recovered", job_id=w.job_id, site=self.site,
                           found="queued", handle=handle)
                return
            if find is None:
                # cannot positively rule out an accepted submission —
                # a blind re-drive could run the task twice
                self._fail(w, WeftError(
                    "site.unreachable",
                    "submit was cut by a site outage and this scheduler "
                    "offers no queue-by-name check — cannot rule out an "
                    "accepted duplicate",
                    stage="submit", retryable=False,
                    hints={"deferral": dfr,
                           "suggestion": "check the scheduler queue for "
                                         f"weft-{w.job_id}, then resubmit"}))
                return
        attempts = int(dfr.get("attempts") or 0)
        self._unregister(w.job_id)
        if attempts >= 1:
            self._fail(w, WeftError(
                "site.unreachable",
                "submit never delivered and the re-drive was cut by an "
                "outage as well",
                stage="submit", retryable=True,
                hints={"deferral": dfr,
                       "suggestion": "resubmit when the site is stable; "
                                     "memoization makes the retry cheap"}))
            return
        dfr["attempts"] = attempts + 1
        store.set_job_deferral(w.job_id, dfr)
        store.emit("job.redriven", job_id=w.job_id, site=self.site,
                   note="deferred submit never delivered; re-driving once")
        import threading as _th
        _th.Thread(target=self.runner._drive, args=(w.job_id,),
                   daemon=True).start()

    # scheduler verdicts: the scheduler POSITIVELY says the job is gone.
    # Strikes exist to guard against absence of signal (a poll that could
    # not see the process); a verdict needs no confirmation — waiting on
    # strikes here left slurm-killed kernels "running" until slurm forgot
    # the job, i.e. forever with accounting on (found by weft-ui; walltime
    # death is the EXPECTED kernel death mode, kernels being
    # walltime-bounded by design).
    _VERDICT_CAUSE = {"timeout": "walltime_exceeded", "oom": "oom",
                      "cancelled": "cancelled"}

    def _lease_transition(self, w: Watch, state: str, status: dict) -> None:
        """Leases (kernels, services) have no COLLECTING: an exit is a
        requested stop or a death — reported, with diagnostics."""
        if state in ("exited", "lost", "missing") \
                or state in self._VERDICT_CAUSE:
            verdict = self._VERDICT_CAUSE.get(state)
            if verdict is None:
                w.lost_strikes += 1
                if state != "exited" and w.lost_strikes < LOST_STRIKES:
                    return
            cause = verdict or ("exited" if state == "exited" else "lost")
            self._unregister(w.job_id)
            if w.lease == "service":
                s = self.runner.store.get_service(w.job_id)
                if not s or s["state"] not in ("starting", "ready"):
                    return  # clean stop already recorded
                log_tail = self.runner.tail_log(self.adapter, w.jobdir_rel, 30)
                self.runner.store.update_service(w.job_id, state="exited")
                self.runner.store.emit(
                    "service.exited", service=w.job_id, site=self.site,
                    cause=cause, slurm_state=status.get("slurm"),
                    exit_code=status.get("exit_code"),
                    log_tail=log_tail[-800:],
                    suggestion="service_status shows the record; "
                               "service_start again after fixing the cause")
                return
            k = self.runner.store.get_kernel(w.job_id)
            if not k or k["state"] != "running":
                return  # clean stop already recorded
            killing, log_tail = None, ""
            try:
                if self.adapter.file_exists(f"{w.jobdir_rel}/current_block"):
                    killing = int(self.adapter.read_file(
                        f"{w.jobdir_rel}/current_block").decode().strip())
                log_tail = self.runner.tail_log(self.adapter, w.jobdir_rel, 30)
            except (WeftError, ValueError):
                pass
            self.runner.store.update_kernel(w.job_id, state="died")
            # receipt + pin settlement off-tick: shim calls must not
            # delay sibling verdicts in this poll round
            import threading as _th

            def _post_mortem():
                self.runner.record_run_inventory(w.job_id, self.site,
                                                 w.jobdir_rel)
                retains = getattr(self.runner, "retains", None)
                if retains is not None:
                    retains.settle_pins(w.job_id)
            _th.Thread(target=_post_mortem, daemon=True).start()
            self.runner.store.emit(
                "kernel.died", kernel=w.job_id, site=self.site,
                **({"label": k["label"]} if k.get("label") else {}),
                cause=cause, slurm_state=status.get("slurm"),
                killing_block=killing, exit_code=status.get("exit_code"),
                log_tail=log_tail[-800:],
                suggestion=("the allocation hit its walltime — "
                            "kernel_restart with a longer walltime "
                            "(and replay='successful') resumes the work"
                            if cause == "walltime_exceeded" else
                            "kernel_restart(kernel_id, replay='successful') "
                            "rebuilds state; skip the killing block"),
            )
            return
        w.lost_strikes = 0
        if w.lease == "service":
            return  # services are bounded by walltime, not idleness
        # idle auto-stop, if the site owner asked for it (policy knob)
        row = self.runner.store.get_site(self.site) or {}
        idle_cap = ((row.get("config") or {}).get("policy")
                    or {}).get("kernel_idle_stop_s")
        if idle_cap:
            k = self.runner.store.get_kernel(w.job_id)
            if k and time.time() - k["last_used"] > float(idle_cap):
                self.adapter.write_file(f"{w.jobdir_rel}/kernel.stop", b"1\n")
                self.adapter.cancel(w.handle, w.jobdir_rel)
                self.runner.store.update_kernel(w.job_id, state="stopped")
                self.runner.store.emit(
                    "kernel.idle_stopped", kernel=w.job_id, site=self.site,
                    idle_s=round(time.time() - k["last_used"], 0),
                    policy_s=idle_cap)
                self._unregister(w.job_id)

    def _fail(self, w: Watch, err: WeftError) -> None:
        self._unregister(w.job_id)
        job = self.runner.store.get_job(w.job_id)
        if job and job["state"] != "CANCELLED":
            self.runner.store.update_job(w.job_id, state="FAILED",
                                         error=err.to_dict())
            self.runner.store.emit("job.failed", job_id=w.job_id,
                                   **self.runner.group_payload(w.array_group),
                                   **err.to_dict())
