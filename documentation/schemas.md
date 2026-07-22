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
| `outputs` | `[{path, ref (dref:…), bytes, preview}]` (+ one tree entry per declared output dir; a declared output that IS a file appears as a plain file entry — both shapes are legal declarations) |
| `output_bytes` | total |
| `logs` | `{tail, site_path}` |
| `transcript` | transcript-manifests only: ordered `[{block, code, rc}]` through the promoted block |
| `session` | session-kernel promotions only: `{session_id, snapshotted_at_promote: true}` — the cited `env_id` is the snapshot minted AT promote (a live session is a moving target; the record never cites one) |

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

## `published:v1` — `env_published(site, tree)`

Render-complete catalog rows: a host UI is a pure projection of these
(no host-side probes or joins). Per version, write-time facts recorded
by `env_publish` (`env_id`, `image_sha256`/`image_bytes`, `glibc_floor`,
`grade`, `spec_summary {spec_name, platforms, packages_per_platform,
deps}`, `notes`, `published_at`; republishing a version heals older
rows) plus read-time truth from the querying workspace: `is_latest`,
`runnable_here` (site glibc vs floor; `null` when the site's glibc is
unknown — unknown ≠ runnable; no floor recorded = no constraint),
`state_here` ∈ {`adopted-ro`, `ready`, `building`, `failed`, `missing`}
from this workspace's realization rows (note: another user's adoption is
invisible here — realization rows are per-workspace), and `last_used`.
`env_status` realizations carry the matching `read_only` flag and the
summary carries the spec `name`.

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

Host metadata envelope (additive): `bundle_export(..., metadata=…)`
accepts bytes or any JSON value and stores it as a separate archive
member (`bundle/host-metadata.{json,bin}`); `bundle_import` returns it
verbatim under `metadata` (None if none was supplied). Sealed: weft
never parses it, it does not enter the bundle's identity or the
re-derivation proof, and re-exporting an imported bundle does NOT carry
an old envelope forward — the envelope belongs to the caller of each
export. Capped at 64 MB: it carries context, not data (data belongs in
blobs, where it is content-addressed and verified).


## ensure_available envelope (pinned contract)

The result envelope of `ensure_available` is a PINNED cross-repo
contract: `documentation/ensure_envelope.schema.json` (versioned;
currently 1). weft guards it with tests/conformance/test_envelope.py
validating REAL envelopes; consumers (aba) mirror the guard in their
conformance suite. Changes are deliberate,
COORDINATED two-repo events: aba's drift guard byte-compares a
vendored copy of the schema file against this checkout, so any edit —
additive included — must land with their vendored copy updated in the
same exchange. Success: {satisfied, changed, attempts,
verified, runtime, session_id | env_id}. Failure: the standard error
envelope with attempts/verified/runtime riding hints. Attempt lanes:
conda|pypi|cran|installer|extends_env; outcomes: installed|
installed_unverified|failed|refused|skipped|solved.
