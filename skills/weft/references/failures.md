# Failure taxonomy & remediation playbook

Every failure: `{"error": code, "stage", "detail", "hints", "retryable"}`.
The hints are designed for a specific recovery — apply it; never resubmit
an unchanged failing task more than once.

| code | what happened | your move |
|---|---|---|
| `env.solve_conflict` | pins unsatisfiable | `hints.solver_message` names them; relax the offending pin in `hints.user_pins`, re-ensure |
| `env.solve_failed` | index/network trouble | retryable — retry once, then tell the user |
| `env.realize_failed` | build/unpack broke on site | read `hints.log_tail`; if corrupt → `env_repair(env_id, site)` + resubmit |
| `env.layer_conflict` | one dep layer contradicts another (e.g. `cran` deps without `r-base` in `deps.conda`) | hints name the missing piece — add it to the spec |
| `env.not_realized` | env exists but was never built on this site (kernels need this) | run any task with the env there first (even `true`) |
| `env.unsatisfiable_on_site` | musl libc, missing module, no runtime | `hints.suggestion` / alternatives; re-place to another site |
| `env.platform_mismatch` | env locked for other platforms than the site's (e.g. linux-64 env, osx-arm64 site) | add `hints.site_platform` to the spec's `platforms`, `env_ensure` again (new EnvID) |
| `internal.error` | a weft bug, not a known failure mode | `hints.traceback_tail` has the trace; a retry may or may not help — tell the user, include the trace |
| `site.capability_violation` | ask exceeds hardware, partition, or **user policy** (`hints.source`) | clamp to `hints.*.max` / `fitting_partitions`, or negotiate with the user — policy is their choice |
| `sched.rejected` | sbatch refused | `hints.stderr`; check partition/account against `sites_describe` |
| `job.walltime_exceeded` | time limit hit | raise `resources.walltime` (hints show asked vs elapsed) or shrink the task |
| `job.oom` | killed for memory | resubmit with `mem_gb` ≥ max(2 × requested, 1.5 × observed peak). The peak UNDERSTATES need when the kill hit during allocation — never size *down* toward it |
| `job.nonzero_exit` | user code failed | `hints.log_signature` (traceback/cuda/mpi/module...) + `log_tail`; fix the actual bug |
| `sched.node_failure` | process vanished, no exit record (crash/reboot) | infrastructure, not code: resubmit once; if repeated, `doctor()` + tell the user |
| `data.verify_failed` | hash mismatch / purged cache | retryable — locations demoted; resubmit re-transfers |
| `data.missing` | unknown/unavailable ref | register the path, or fetch from the site that holds it |
| `site.unreachable` | transport down | wait — detached jobs survive; watch for `site.reachable`; don't fail anything yourself |
| `budget.exceeded` | cloud cap (pre-launch or watchdog) | never loop; report spend from hints and ask the user about the cap |
| `quota.storage` | site disk pressure | suggest GC / another storage root to the user |
| `task.invalid` | malformed request | fix your call; hints list valid fields/values |
| `task.dep_failed` | an `after` dependency failed/vanished — this job never ran | fix + re-run the upstream job (`hints.dependency`), then resubmit this one |
| `env.unavailable_in_lanes` | every lane YOU ranked ran to a verdict and none provided it | read `hints.attempts` — each attempt carries its own lane's typed error and levers; there is no single fix |
| `env.evict_blocked` | overlay children or live jobs/sessions/kernels use this env | hints name them: cascade=True for children, cancel/stop for live work |

Session install failures discriminate: a dead package index is
`env.solve_failed` (retryable — the packages are not missing, the index
is), a package absent from the snapshot/repos is `env.solve_conflict`,
and a broken compile is `env.realize_failed`; R-lane hints carry
`install_rc` AND `verify_rc` separately (an rc of 0 in a failure means
the OTHER stage failed) plus the verifier's `missing` line.

Kernel deaths are events, not job errors: **`kernel.died`** carries the
killing block and log tail; recover with
`kernel_restart(k, replay="successful")` (returns a NEW kernel_id).

`FAILED` jobs keep their full error under `task_result(job_id)`; classified
log signatures ride in `hints.log_signature.all_signatures` when several
patterns matched. From any finished result, `provenance(job_id | dref)`
reconstructs the full chain (command, exact env layers/SHAs/snapshots,
inputs, producing jobs) — use it before asserting anything about how an
artifact was made. Its `placement` block answers WHERE it ran as facts —
{site, node, allocation_id, partition, node_truth (probe-derived,
source-labeled), ran_at} — kept separate from that reproducibility
closure: placement is circumstance, never identity (a rerun elsewhere
memoizes the same).

## Live-job triage (the node hop)

A RUNNING job that looks stuck is diagnosable from INSIDE its allocation:
`job_node_exec(job_id, "ps -o pid,etime,comm -u $USER; free -m", why=...)`
joins the job's node via `srun --overlap` — live GPU telemetry
(`nvidia-smi`), memory pressure, node-local scratch. Audited and
deny-listed like `site_exec`; works only while the job runs. Triage order:
`task_logs` (cheap, from the shared FS) → `job_node_exec` (the node's own
view) → `task_cancel` with cause. If several jobs on one PARTITION act up,
`site_probe_deep(site, partitions=[...])` re-measures what its nodes
actually are (egress, GPUs, glibc) — capability drift shows up there.
