---
name: weft
description: >
  Drive weft — the execution substrate that runs compute tasks on the
  user's machines (local, SSH workstations, Slurm clusters, cloud) with
  reproducible environments and content-addressed data. Use when the user
  wants an analysis step executed somewhere, an environment built, data
  staged or fetched, cluster/queue state inspected, or a failed remote job
  diagnosed. Triggers: "run this on the cluster", "offload", "submit",
  "build an env with X", "why did my job fail", "how busy is the cluster".
---

# Driving weft

Weft turns "the user has compute somewhere" into "you can use it,
reproducibly, with a paper trail". You hold a `Weft` instance (one per
project workspace); every method returns plain JSON-able data.

```python
from weft.api import Weft
w = Weft(workspace_dir)          # pixi/pixi-pack auto-found next to pixi_bin
```

## The mental model (5 objects)

- **Site** — a registered place to run (`local` | `ssh` | `slurm` | `cloud`),
  described by a *probed capability record* + live `site_load()`.
- **EnvSpec → EnvID** — declarative environment, solved & locked once;
  the EnvID (`env:v1:…` conda/pypi-only, `env:v2:…` with extra layers) is
  the universal cache key. Never install imperatively. Mid-analysis "add
  one package": `{"extends_env": <EnvID>, "deps": {"pypi": [...]}}` —
  base frozen by construction, realizes in seconds as an overlay on the
  parent's prefix.
- **DataRef** — `dref:<sha256>` for every file/tree. Content moves at most
  once per site; outputs chain site-side for free.
- **Task** — env + inputs + command + outputs; content-hashed, so identical
  resubmissions return the recorded manifest (memoization) unless `force`.
  Three execution shapes share this model: **tasks** (run→manifest),
  **kernels** (persistent interpreter, block-fed), **services** (run until
  stopped, publish an endpoint).
- **Job** — a task in flight; you watch *events*, never block.

## The golden path

```python
env = w.env_ensure({"name": "fit", "deps": {"conda":
      ["python =3.12", "numpy", "iminuit"]}})["env_id"]   # solved|cached
ref = w.data_register("raw/run2189.csv")["ref"]
r = w.task_submit({
    "command": "python fit.py --data data/run.csv --out results/",
    "env": env,
    "inputs": [{"ref": ref, "mount_as": "data/run.csv"}],
    "code": {"ref": w.data_register("fit.py")["ref"], "mount_as": "fit.py"},
    "outputs": ["results/"],
    "resources": {"cpus": 8, "mem_gb": 16, "walltime": "01:00:00"},
    "site": "auto",              # ranked placement with reasons
})
r["plan"]        # relay to the user if costly: staging bytes, env action,
                 # queue, site_policy_notes (user's own rules — respect them)
feed = w.events_poll(cursor)     # job.state / array.progress / transfer.*
m = w.task_result(job_id)        # manifest: outputs with previews
w.data_fetch(ref, "local/path")  # only when previews aren't enough
```

## Operating doctrine

1. **Plans before effects.** Relay the submit plan for anything costly
   (big staging, env build, long walltime). Cloud launches cost money and
   are budget-gated — never loop retries against `budget.exceeded`.
2. **Read the error, don't retry blind.** Every failure is
   `{error, stage, detail, hints, retryable}`. The hints name the fix —
   apply it (see references/failures.md). Never resubmit an unchanged
   failing task more than once.
3. **Keep bulk data remote.** Reason over previews and digests; fetch
   selectively. Outputs feeding later tasks never need to leave the site —
   and cross-SITE inputs route directly (shared FS or dst-pulls-from-src)
   when a probed route exists; the plan's `staging.site_to_site` says so.
4. **Adapt freely; let weft label it.** Prefer the clean path (new package
   → new spec → new EnvID), but **escape hatches are normal, supported
   moves**: patch a package, build from source, run a bespoke installer,
   relax a pin. When you take one, weft **grades** the result
   (`fully-pinned` → `snapshot-pinned` → `attested` → `escape-hatch` →
   `state-dependent`) and you record *why* in the spec's `notes` /
   `step_notes` (identity-neutral — annotating never forks the EnvID).
   Getting the user unblocked *now*, with the soft step visible, beats a
   perfect env that never runs. Explore in sessions; snapshot when a
   result is worth keeping.
5. **Respect user policy.** `sites_list()` shows per-site rules and notes
   ("don't use during the day"); weft enforces the structured ones, you
   honor the prose ones.
6. **When confused, look.** `doctor()` (multi-hop sites: which hop died),
   `site_load()`, `task_logs()`, `site_exec(site, cmd, why=...)` and
   `job_node_exec(job_id, cmd, why=...)` — INSIDE a running job's
   allocation (both audited, deny-listed) — `reconcile()` after a
   controller restart. Cancel with a cause: `task_cancel(id, why=...)`.
7. **On a new cluster, discover before you submit.** `sites_describe`
   (partitions with GRES/features/limits) → `site_associations` (MY
   QOS/GPU ceilings) → `module_list(search=...)` → `site_probe_deep`
   (compute-node truth, measured egress) → per-partition ETAs via
   `site_load(resources=, partitions=[...])`. Write what you learn to the
   site notebook (`site_note`) — it outlives your session.
8. **Chain, don't poll.** `after=[job_ids]` sequences pipeline stages
   (failed upstream → `task.dep_failed`, downstream never runs); big
   sweeps triage by `failure_buckets`, page with `array_elements`, heal
   with `array_retry`. A finished result travels as a bundle
   (`bundle_export` → `bundle_import` → re-run proves re-derivation).

## References

- `references/sites.md` — registering local/ssh/slurm/cloud (incl. bastion
  `jump` chains and read-only institutional roots), policy blocks,
  capability probing + compute-node probe-jobs, associations/QOS ceilings,
  live load & per-partition ETAs, module discovery, the site notebook,
  budgets, teardown.
- `references/environments.md` — spec schema, layering (`extends` /
  `extends_env` + overlay realization), sessions+snapshot, GPU/CUDA
  pinning, realization strategies, repair, reuse, eviction semantics.
- `references/data.md` — DataRefs, staging plans, chaining, fetch,
  transfer progress, chunked big files, site-to-site routing (shared-FS
  links / direct pulls — the controller carries bytes only as a last
  resort), reproducibility bundles.
- `references/jobs.md` — lifecycle, arrays & digests & retry, monitoring,
  queue reasons, logs, cancel, memoization, provenance.
- `references/kernels.md` — persistent interactive interpreters
  (python/R/julia): incremental blocks with live state, interrupt, crash
  recovery with transcript replay, promotion into the record.
- `references/services.md` — long-lived endpoint-publishing processes
  (dashboards, notebook servers, query APIs near the data): tunneled,
  readiness-checked, death-reported.
- `references/failures.md` — full error taxonomy with the remediation
  playbook; crash/outage semantics.
- `references/scenarios.md` — worked end-to-end patterns (offload,
  cluster scan, GPU burst, compile-from-source, exploration).
