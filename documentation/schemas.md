# Versioned output schemas

Hosts building their own durable record ingest these shapes; the `schema`
field is the compatibility contract (same discipline as `env:v1`/`env:v2`
identities). Additive fields may appear within a version; renames/removals
bump it, with a note here.

## `manifest:v1` — one per finished job (`task_result`)

| field | meaning |
|---|---|
| `schema` | `"manifest:v1"` |
| `reproducibility` | graded confidence, worst-rung-wins: `"fully-pinned"` (every package content-hashed) → `"snapshot-pinned"` (dated snapshots / commit SHAs; reproduces almost always) → `"attested"` (site modules or the bare site env; unpinnable) → `"escape-hatch"` (a post_install / session installer ran) → `"state-dependent"` (kernel-promoted from interpreter state; replay the transcript) |
| `reproducibility_meaning` | one sentence explaining the grade |
| `reproducibility_components` | per-component breakdown `[{component, grade, why}]` — which step is the soft one |
| `job_id`, `task_hash`, `env_id`, `site` | identities; `task_hash` doubles as the memoization key |
| `node` | hostname captured ON the executing node (best-effort; `null` if the runner predates it). Circumstance, not identity — it never enters `task_hash` |
| `exit_code`, `wall_s`, `max_rss_gb` | run facts |
| `outputs` | `[{path, ref (dref:…), bytes, preview}]` (+ one tree entry per declared output dir) |
| `output_bytes` | total |
| `logs` | `{tail, site_path}` |
| `transcript` | transcript-manifests only: ordered `[{block, code, rc}]` through the promoted block |

## `provenance:v1` — `provenance(job_id | dref)`

Job node: `schema`, `reproducibility` (from its manifest), `job_id`,
`state`, `site`, `task_hash`, `command`, `env_vars`,
`outputs [{path, ref}]`, `placement` (below), `environment` (below),
`inputs` — each input is
`{mount_as, ref, bytes, origin, produced_by?}` where `produced_by`
recurses into the producing job (depth-limited).

`placement` — WHERE the job ran, as first-class facts, deliberately
distinct from the node-agnostic reproducibility closure (placement is
circumstance, never identity: a rerun elsewhere memoizes the same):
`site`, `node` (from the manifest), `allocation_id` (the scheduler
handle, e.g. `slurm:12345`; for kernel-promoted records, the kernel's
allocation — that IS where the blocks ran), `partition`,
`ran_at {wall_s, collected_at}`, and `node_truth` — the probe-derived
facts that mattered (`glibc`, `gpus`, `cuda_driver`) labeled with their
`source` (deep probe of the job's partition when available, else the
site probe). `node_truth` is a probe record, not a per-job measurement.

`environment`: `env_id`, `spec` (the exact stored spec body),
`weakly_reproducible`, `notes` / `step_notes` (the agent's rationale for
adaptive steps — identity-neutral, so annotating never forks the EnvID),
`modules_attested` (site-provided, named but not content-pinned),
`post_install`, `layers` — per ecosystem:
`{packages, snapshot?, pinned_shas {name: sha}}`.

When a job ran under a revised environment (site policy `on_drift:
"revise"`), the manifest's `env_id` is the **effective** env and a
`job.env_revised` event carries `{requested, effective, diff}` — the old
EnvID is never silently redefined.

Ref node (when the target is a `dref:`): `ref`, `bytes`, `origin`
(user path, URL, or `job:…`), `produced_by?`.

Guarantee: every field needed to regenerate a result — or to state
honestly why regeneration isn't content-pinned — is in these two shapes.


## `capabilities:v2`

Versioned site capability record. Top level = where the control plane
runs (login node / direct host); `scheduler.partitions[*]` each carry
`gres` ([{type, model, count}]), `features`, `max_walltime` +
unambiguous `max_walltime_s`, scontrol detail (default_walltime,
priority_tier, oversubscribe), and — after `site_probe_deep` — a
`compute` sub-record measured ON a node of that partition (same schema,
`measured_on`/`probed_at` provenance, measured `internet`). Fields set by
`capabilities_override` are listed in `overridden_fields`: declared, not
measured. `storage.candidates` = probed [{path, writable, free_gb}].

## `bundle:v1`

One-file provenance closure of a finished job: `target_job`, `jobs`
(task + task_hash + manifest per job in the input-producing closure),
`envs` (spec body + spec_hash + canonical lock + native lockfile +
platforms + parent/layerable), `datarefs` (rows incl. chunk lists), and
content under `bundle/blobs/<sha256>` (verified on import) +
`bundle/trees/<hash>.json`. Contract: `bundle_import` into any workspace,
re-run the returned task with force=True — output refs must equal
`recorded_outputs` (the re-derivation proof). `reproducibility` carries
the honest limits (escape-hatch re-executes captured installers; attested
modules must exist at the destination).
