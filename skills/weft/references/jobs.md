# Jobs & monitoring

Lifecycle: `PENDING → RESOLVING_ENV → STAGING → [QUEUED] → RUNNING →
COLLECTING → DONE | FAILED | CANCELLED`. Nothing blocks: submit returns a
plan + job_id; drain `events_poll(cursor)` on your turns.

- `task_status(job_id)` / `task_status(state="RUNNING")` — snapshots.
- `task_logs(job_id, tail=100)` — fetched on demand from the site.
- `task_result(job_id)` — the manifest (outputs with refs+previews,
  exit_code, wall_s, max_rss_gb, log tail with classified signature).
- `task_cancel(job_id)` — works queued or running; external `scancel`
  is detected and recorded as CANCELLED.
- Monitoring is batched per site (one scheduler query per tick regardless
  of job count) — submit thousands of elements without guilt; per-site
  `policy.poll_interval_s` controls cadence.

## Arrays

`"array": N` fans out N element jobs with `WEFT_ARRAY_INDEX` ∈ [0,N).
Watch the **digests**, not the elements: `array.progress` events carry
`{done, failed, running, queued, failed_previews}` coalesced per change;
`array.done` carries the roll-up (wall-time stats, failures, output
bytes). `events_poll` hides element-level events by default
(`compact=False` to see all). `array_status(group)` / `array_result(group)`
on demand. To retry a failed element, fetch its task from
`array_status(...)["failed_previews"]` → `store.get_job(job_id)["task"]`
and resubmit with `force=True` (a first-class retry API is on the roadmap).

## Crash & outage semantics (what you can rely on)

- Site unreachable → ONE `site.unreachable` event; detached jobs keep
  running; polling backs off and recovers (`site.reachable` has the
  outage duration). Never treat an outage as a job failure.
- Controller restarted → call `reconcile()`; jobs with handles resume
  watching, unfinished submissions re-drive, completed-during-the-gap jobs
  get collected. `doctor()` first if unsure.
- Remote crashed/rebooted → the job fails `sched.node_failure` ("crash or
  reboot") after two confirming polls — pid recycling cannot fake a
  running job (process identity is checked, not just the pid).
- Every submission/cancel/diagnostic is in the audit log with its why:
  `store.audit_tail(50)` answers "what ran last night".
