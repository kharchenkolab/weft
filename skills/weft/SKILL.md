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
  the universal cache key. Never install imperatively.
- **DataRef** — `dref:<sha256>` for every file/tree. Content moves at most
  once per site; outputs chain site-side for free.
- **Task** — env + inputs + command + outputs; content-hashed, so identical
  resubmissions return the recorded manifest (memoization) unless `force`.
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
   selectively. Outputs feeding later tasks never need to leave the site.
4. **Extend specs, don't mutate envs.** New package → new spec (or
   `extends` layer) → new EnvID. For interactive exploration use a session
   env, then `session_snapshot` before recording results.
5. **Respect user policy.** `sites_list()` shows per-site rules and notes
   ("don't use during the day"); weft enforces the structured ones, you
   honor the prose ones.
6. **When confused, look.** `doctor()`, `site_load()`, `task_logs()`,
   `site_exec(site, cmd, why=...)` (audited, deny-listed), `reconcile()`
   after a controller restart.

## References

- `references/sites.md` — registering local/ssh/slurm/cloud, policy blocks,
  capability probing, live load & queue/ETA, modules, budgets, teardown.
- `references/environments.md` — spec schema, layering, sessions+snapshot,
  GPU/CUDA pinning, realization strategies, repair, reuse semantics.
- `references/data.md` — DataRefs, staging plans, chaining, fetch,
  transfer progress, chunked big files.
- `references/jobs.md` — lifecycle, arrays & digests & retry, monitoring,
  queue reasons, logs, cancel, memoization, provenance.
- `references/kernels.md` — persistent interactive interpreters
  (python/R/julia): incremental blocks with live state, interrupt, crash
  recovery with transcript replay.
- `references/failures.md` — full error taxonomy with the remediation
  playbook; crash/outage semantics.
- `references/scenarios.md` — worked end-to-end patterns (offload,
  cluster scan, GPU burst, compile-from-source, exploration).
