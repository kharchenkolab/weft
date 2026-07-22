# Jobs & monitoring

Lifecycle: `PENDING → RESOLVING_ENV → STAGING → [QUEUED] → RUNNING →
COLLECTING → DONE | FAILED | CANCELLED`. Nothing blocks: submit returns a
plan + job_id; drain `events_poll(cursor)` on your turns.

- `task_status(job_id)` / `task_status(state="RUNNING")` — snapshots.
- `task_logs(job_id, tail=100)` — fetched on demand from the site.
- `task_result(job_id)` — the manifest (outputs with refs+previews,
  exit_code, wall_s, max_rss_gb, log tail with classified signature).
- Declared `outputs` may be files OR directories: `["plot.svg"]` works
  (no mkdir boilerplate — the single figure/table is the common step
  output); `results/` stays the convention for multi-artifact steps.
  An output that is neither file nor directory at collection fails the
  job with "was not produced".
- `task_cancel(job_id)` — works queued or running; external `scancel`
  is detected and recorded as CANCELLED. Cancels are confirmed against
  the scheduler before the job settles: if a poll still shows it live,
  the cancel is resent (`job.cancel_retry` event) — expect a short lag,
  not an instant CANCELLED.
- Monitoring is batched per site (one scheduler query per tick regardless
  of job count) — submit thousands of elements without guilt; per-site
  `policy.poll_interval_s` controls cadence.

## Arrays

`"array": N` fans out N element jobs with `WEFT_ARRAY_INDEX` ∈ [0,N).
Watch the **digests**, not the elements: `array.progress` events carry
`{done, failed, running, queued, failed_previews}` coalesced per change;
`array.done` carries the roll-up (wall-time stats, failures, output
bytes). `events_poll` hides element-level events by default
(`compact=False` to see all). `array_status(group)` / `array_result(group)` on demand
(the digest events use the key `array_group`; the APIs take the same
value as `group`). Memoized elements count in the digests like any other.

**Retrying failed elements** is one call — retries rejoin the group under
their index, digests heal, and a fresh `array.done` is emitted:

```python
w.array_retry(group)                            # all failed elements
w.array_retry(group, indices=[3, 17])           # specific ones
w.array_retry(group, command_override="...")    # with a fixed command
```

A replaced element's old row leaves the group's counts but stays
queryable (`jobs_where`) with `superseded_by` naming its successor —
fold those under the group's history, they are not duplicates.

One-off scheduler needs ride `resources.scheduler_directives`
(e.g. `["--constraint=a100"]`) — raw #SBATCH lines, validated (managed
flags like --partition/--time are refused naming the structured lever;
identity flags always refused). Site-wide quirk fixes belong in site
config `scheduler.extra_directives` / `site_prelude` instead: set once,
every job inherits.

Give tasks a `label` ("calibrate run 3", ≤200 chars) — it shows in
`task_status`/`jobs_where`/`array_status` and on the PENDING event, and
is NOT part of task identity: relabeling never forks memoization, and a
memoized resubmission returns the prior job under its original label.

**Enumeration** (what exists in this workspace): `jobs_where(state=,
site=, limit=, offset=)`, `list_envs()`, `list_kernels()`,
`list_services()`, `audit_tail(n)`. `task_status(job_id)` also carries
`plan` — the persisted submit-time promise (staging bytes, env action)
to compare against what actually happened; arrays store one plan for
the group (`array_status(group)["plan"]`).

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

## Following a running job's log

```python
out = w.task_logs(job_id, follow_cursor=0)     # then keep passing back
out["log"], out["cursor"], out["state"]        # the returned cursor
```
Byte-exact and gap-free; stop when state goes terminal. Plain
`task_logs(job_id, tail=100)` for a one-shot look.

## Pipelines: `after` (control-flow chaining)

`task_submit({..., "after": [job_a, job_b]})` holds the job until every
dependency is DONE — no polling between stages. A failed/cancelled
upstream fails the downstream job as `task.dep_failed` (it NEVER runs on
missing inputs), with the culprit and its error in the hints. Dependencies
gate WHEN a task runs, not WHAT it computes: they are excluded from the
task hash, so memoization still works stage by stage. Unknown job_ids are
refused at submit. Chain data the usual way (site-side output→input
chaining); `after` only sequences.

## Small print

- `task_status(job_id)` returns a LIST (uniform with the state-filter
  form) — take `[0]` for a single job.
- `task_cancel(job_id, why="hung: no output 20min")` — give the cause;
  it lands in the audit trail and the returned payload.
- `dry_run=True` runs placement + capability/policy checks and returns
  the plan (including `resources` as understood — check the partition
  landed) without submitting.

## Large sweeps: triage by bucket, not by element

`array_status(group)` clusters FAILED elements by log signature
(`failure_buckets`: signature, count, sample indices) — a 2000-element
sweep with three failure modes reads as three lines. Above ~200 elements
the per-element list is not inlined; page it with
`array_elements(group, state="FAILED", offset=, limit=)`. Fix and
`array_retry(group)` (or `indices=[...]`, optional `command_override=`):
retries REJOIN the group under their index — digests and `array_result`
heal; superseded rows drop out of the counts.
