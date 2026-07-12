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
    kernel: bool = False         # kernel watches report deaths, not results


class SitePoller:
    def __init__(self, site: str, adapter, runner):
        self.site = site
        self.adapter = adapter
        self.runner = runner
        self._watches: dict[str, Watch] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
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

    # -- loop ------------------------------------------------------------------

    def _run(self) -> None:
        idle = 0
        while True:
            self._wake.wait(timeout=self._backoff or self._interval())
            self._wake.clear()
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

    def _tick(self, items: list[Watch]) -> None:
        try:
            statuses = self.adapter.poll_jobs(
                [(w.handle, w.jobdir_rel) for w in items]
            )
        except WeftError as e:
            if e.code != "site.unreachable":
                # a site-fatal error (e.g. budget.exceeded tore the cloud
                # instance down) kills every job watched here — burying it
                # as a poller.error would leave them RUNNING forever
                for w in items:
                    self._fail(w, e)
                    if w.array_group:
                        self.runner.emit_group_digest(w.array_group)
                return
            # one outage, one event — regardless of how many jobs wait it out
            if self._outage_since is None:
                self._outage_since = time.time()
                self.runner.store.set_health(self.site, "unreachable")
                self.runner.store.emit("site.unreachable", site=self.site,
                                       jobs_waiting=len(items))
            self._backoff = min(max(self._backoff * 2, self._interval() * 2),
                                OUTAGE_BACKOFF_CAP_S)
            return
        if self._outage_since is not None:
            self.runner.store.set_health(self.site, "ok")
            self.runner.store.emit(
                "site.reachable", site=self.site,
                outage_s=round(time.time() - self._outage_since, 1),
            )
            self._outage_since = None
        self._backoff = 0.0

        dirty_groups: set[str] = set()
        for w in items:
            try:
                self._transition(w, statuses.get(w.handle, {"state": "unknown"}))
            except WeftError as e:
                self._fail(w, e)
            except Exception as e:
                self._fail(w, WeftError(
                    "state.conflict", f"internal poller error: {e!r}",
                    stage="running",
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
        if w.cancelled:
            self.adapter.cancel(w.handle, w.jobdir_rel)
            self._unregister(w.job_id)
            return
        state = status.get("state")
        if w.kernel:
            self._kernel_transition(w, state, status)
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

    def _kernel_transition(self, w: Watch, state: str, status: dict) -> None:
        """Kernels have no COLLECTING: an exit is either a requested stop
        or a death — and the *block that killed it* is the diagnostic."""
        if state in ("exited", "lost", "missing"):
            w.lost_strikes += 1
            if state != "exited" and w.lost_strikes < LOST_STRIKES:
                return
            self._unregister(w.job_id)
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
            self.runner.store.emit(
                "kernel.died", kernel=w.job_id, site=self.site,
                killing_block=killing, exit_code=status.get("exit_code"),
                log_tail=log_tail[-800:],
                suggestion="kernel_restart(kernel_id, replay='successful') "
                           "rebuilds state; skip the killing block",
            )
            return
        w.lost_strikes = 0

    def _fail(self, w: Watch, err: WeftError) -> None:
        self._unregister(w.job_id)
        job = self.runner.store.get_job(w.job_id)
        if job and job["state"] != "CANCELLED":
            self.runner.store.update_job(w.job_id, state="FAILED",
                                         error=err.to_dict())
            self.runner.store.emit("job.failed", job_id=w.job_id,
                                   **self.runner.group_payload(w.array_group),
                                   **err.to_dict())
