# Versioned output schemas

Hosts building their own durable record ingest these shapes; the `schema`
field is the compatibility contract (same discipline as `env:v1`/`env:v2`
identities). Additive fields may appear within a version; renames/removals
bump it, with a note here.

## `manifest:v1` — one per finished job (`task_result`)

| field | meaning |
|---|---|
| `schema` | `"manifest:v1"` |
| `reproducibility` | the ladder: `"task"` (content-pinned: locked env + hashed inputs + command), `"transcript"` (replayable ordered kernel blocks — from `kernel_promote`), `"weak"` (env used `post_install`; effects not content-pinned) |
| `job_id`, `task_hash`, `env_id`, `site` | identities; `task_hash` doubles as the memoization key |
| `exit_code`, `wall_s`, `max_rss_gb` | run facts |
| `outputs` | `[{path, ref (dref:…), bytes, preview}]` (+ one tree entry per declared output dir) |
| `output_bytes` | total |
| `logs` | `{tail, site_path}` |
| `transcript` | transcript-manifests only: ordered `[{block, code, rc}]` through the promoted block |

## `provenance:v1` — `provenance(job_id | dref)`

Job node: `schema`, `reproducibility` (from its manifest), `job_id`,
`state`, `site`, `task_hash`, `command`, `env_vars`,
`outputs [{path, ref}]`, `environment` (below), `inputs` — each input is
`{mount_as, ref, bytes, origin, produced_by?}` where `produced_by`
recurses into the producing job (depth-limited).

`environment`: `env_id`, `spec` (the exact stored spec body),
`weakly_reproducible`, `modules_attested` (site-provided, named but not
content-pinned), `post_install`, `layers` — per ecosystem:
`{packages, snapshot?, pinned_shas {name: sha}}`.

Ref node (when the target is a `dref:`): `ref`, `bytes`, `origin`
(user path, URL, or `job:…`), `produced_by?`.

Guarantee: every field needed to regenerate a result — or to state
honestly why regeneration isn't content-pinned — is in these two shapes.
