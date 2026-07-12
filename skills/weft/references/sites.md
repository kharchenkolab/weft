# Sites

Registration is a **user-confirmed** action (their credentials, their
machines). Weft never stores keys — it invokes the system `ssh`, so
aliases, ProxyJump, and MFA work as-is.

```python
w.register_site("local", "local", {"root": "/path/site-root",
                                   "pixi_source": "/path/to/pixi"})

w.register_site("beamlab", "ssh", {
    "host": "beamlab",                  # ~/.ssh/config alias is fine
    "root": "/data/me/.weft",           # everything weft places lives here
    "pixi_source": "/path/to/pixi",     # pushed once, hash-verified
})

w.register_site("hpc", "slurm", {
    "host": "login.hpc.example.edu", "root": "/scratch/me/.weft",
    "pixi_source": "...",
    "scheduler": {"account": "phys-lab", "partition": None},
    "modules_init": "export MODULEPATH=/opt/site-modules",  # site quirk knob
    "capabilities_override": {"compute": {"internet": False}},  # if probe can't see it
    "policy": {   # user rules: structured ones ENFORCED, notes SURFACED
        "partitions_allowed": ["standard", "short"],
        "max_gpus": 4, "max_concurrent_jobs": 50,
        "poll_interval_s": 5,
        "storage": {"large": "/groups/x/me", "scratch": "/scratch/me",
                    "node_tmp": "/tmp"},   # → WEFT_STORAGE_* in every job
        "notes": ["prefer nights for >1h jobs"],
    },
})

w.register_site("cloud-gpu", "cloud", {
    "provisioner": "skypilot",
    "budget": {"max_usd": 20, "max_hours": 2},   # HARD caps
    "resources": {"cpus": 8, "mem_gb": 32, "cuda_driver": "12.4",
                  "gpus": [{"model": "A100-40GB", "count": 1}]},
})
```

## Knowing a site

- `sites_list()` — one line per site: health, cpus/mem/gpus, scheduler,
  internet, policy. `sites_describe(name)` — the full capability record
  (`capabilities:v2`): partitions with limits **plus `gres` (GPU
  type/model/count) and `features` (avx512, ib, …) and scontrol detail
  (default walltime, priority tier, oversubscribe)**; runtimes; storage
  incl. probed `candidates` (path/writable/free_gb); glibc — `"musl"`
  means conda-forge envs are impossible there. `overridden_fields` names
  facts that came from config, not measurement.
- `site_probe(name)` — re-probe after drift (quota moved, module renamed).
- **`site_associations(name)`** — what am *I* allowed to ask for: my
  accounts, allowed/default QOS per partition, structured QOS ceilings
  (`limits_per_user`: cpu/gpu/mem), fairshare factor. All None when the
  cluster hides accounting — unknown is not unlimited.
- `module_check(site, ["espresso/7.2"])` — verify names you know, cached.
  **`module_list(site, search="cuda")`** — DISCOVER what the site offers
  (the first thing to run on a new cluster).
- **`site_load(name, resources=None, fresh=False, partitions=None)`** —
  what is free *now*: host `load_fraction`, free memory; on Slurm also
  per-partition `cpus_idle/allocated/total`, **`gpus_idle/allocated/
  total`**, `pending_jobs`, `my_jobs`, `qos`/`my_associations`/`fairshare`
  (None = no accounting DB, not "no limits"), and with `resources=` a
  scheduler `start_estimate` from `sbatch --test-only` (nothing is
  submitted) — pass `partitions=[...]` for a per-partition
  `start_estimates` comparison ("shortest queue for 2 GPUs right now").
  Use this before choosing where a big campaign goes; `site: "auto"`
  already folds load into its ranking, with reasons.
- GPU asks validate against partition GRES (login nodes have no GPUs);
  refusals name the fitting partitions and the honest ceiling.

## Cloud money rules

Nothing is provisioned at registration or planning. The first control
touch launches (after a budget pre-check: rate × max_hours must fit
max_usd, else `budget.exceeded` and nothing exists to pay for). A runaway
watchdog re-checks accrued spend on every poll: on breach it cancels jobs,
**terminates the instance**, then errors. `site_teardown(name)` is the
explicit off switch; relay `cloud.launched` / `budget.watchdog` events to
the user — they carry the dollars.

## Cache hygiene

`gc_plan()` shows what's reclaimable everywhere (idle realizations, stale
cached data; provenance-referenced content is pin-protected in the
workspace); `gc_sweep(site, confirm=True)` executes — never implicit, and
evicted content re-stages/rebuilds automatically on next use.
`gc_events(older_than_days=30)` prunes the event log (terminal digests
kept). `policy.gc_idle_days` and `policy.kernel_idle_stop_s` tune the
knobs per site; `doctor()` nags about idle kernels and bloat.

## Shared sites (team caches)

Set `"shared": true` when the site root lives on a filesystem several
people can write (a group scratch allocation). weft then creates
group-writable files and takes a **site-side lease** around environment
builds, so two users racing the same EnvID cooperate: one builds, the
other waits and adopts (`realize.adopted`). Trust is the filesystem's —
weft brokers no identity. Env reuse across users is the payoff.

## Reclaiming disk (env footprint)

```python
w.site_footprint("hpc")   # prefixes vs shared package cache vs data cache,
                          # per-env bytes and idle_days
w.env_evict(env_id, "hpc")                 # drop the prefix; cache stays warm
                                           # → rebuild is SECONDS, offline
w.env_evict(env_id, "hpc", archive=True)   # + keep a blob on the CONTROLLER
                                           # → reclaims ~everything; rebuilds
                                           #   with no site network
w.gc_packages("hpc", confirm=True)         # the SHARED cache — consequential:
                                           # rebuilds then need the index
w.gc_orphans("hpc", confirm=True)          # leftovers no record claims
```
`site_footprint` reports `free_bytes` (the premise), what each area costs,
and every ready realization as `evictable`. `env_evict`'s `freed_bytes` is
measured from the filesystem — `apparent_bytes` is the prefix size, most of
which is hardlinked from the shared cache and therefore not reclaimed by
dropping the prefix alone.
Realized envs are the bulk of a quota; the lock that re-materializes them
is kilobytes. Strip aggressively — `env_evict` is cheap to undo.
