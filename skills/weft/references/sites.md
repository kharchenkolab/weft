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

# hosts reachable only from inside (the usual alien-cluster shape:
# internet OUT, ssh-only IN) — model the hops, don't hide them in
# ssh_opts: weft carries your keys/options to EVERY hop and `doctor`
# reports WHICH hop died ("chain breaks at user@bastion")
w.register_site("inner", "ssh", {
    "host": "node7.cluster.internal", "root": "/data/me/.weft",
    "jump": ["me@bastion.univ.edu", "me@login.cluster.edu"],
    "pixi_source": "...",
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
- **`site_route_probe(src, dst)`** — how bytes can move BETWEEN two sites
  without the controller: shared filesystem (nonce visibility) or direct
  ssh with the user's own keys. Probed automatically at registration;
  `sites_describe` lists routes (`via: shared-fs | direct-ssh |
  controller`). Staging uses them transparently — see data.md
  "Site-to-site routing".
- **`site_probe_deep(name, partitions=[...])`** — COMPUTE-NODE truth: runs
  the probe as a tiny job on each partition and records what the nodes
  actually are (GPUs, glibc, **measured egress** — "login has internet"
  says nothing about nodes). Fills per-partition `compute` records;
  realization strategy then keys on measured node egress. Run it once
  after registering a cluster; re-run on drift.
- **`job_node_exec(job_id, cmd, why=...)`** — run a diagnostic INSIDE your
  running job's allocation (`srun --overlap`): live `nvidia-smi` on the
  node your job occupies, `ps`/`free` when it looks stuck, peek at
  node-local scratch. Audited + deny-listed like site_exec; only while
  the job RUNS (the allocation is the access).
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
- **Slurm memory trap**: on clusters without `DefMemPerCPU`, a job that
  sets no `mem_gb` is granted the node's ENTIRE memory — your other jobs
  then pend on "Resources" while CPUs sit idle. If jobs serialize
  unexpectedly, check `task_status(...)['queue_reason']` and set
  `resources.mem_gb` explicitly.
- **`site_note(site, "...")`** — the site notebook: persist operational
  knowledge ("gcc lives in ~/toolchains", "module load broken on gpu
  partition") so it survives your session. Notes ride along in
  `sites_describe` (`site_notebook`, newest last). Write one whenever you
  learn something about a site the hard way — the next agent (or you,
  tomorrow) starts from it.

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

## SSH site config schema (all keys)

`host` (required; a ~/.ssh/config alias works), `root` (required: weft's
site directory), `user`, `port`, `ssh_opts` (raw ssh flags, e.g.
`["-i", "/path/key", "-o", "StrictHostKeyChecking=no"]`),
`jump` (hop list, entries `"user@host:port"` — port optional),
`peer_host`/`peer_port` (the address OTHER SITES use to reach this one,
when NAT or port maps make it differ from the controller's view — feeds
direct site-to-site pulls), `pixi_source`, `pixi_unpack_source`,
`shared` (cross-user root), `capabilities_override`, `policy`. Slurm
adds `scheduler` ({account, partition}) and `modules_init`.

## Institutional / managed roots (read-only base envs)

When base environments are installed by an admin or service account and
users may only READ them, list those roots in the site config:

```python
w.register_site("hpc", "slurm", {..., "root": "/scratch/me/.weft",
                                 "ro_roots": ["/opt/team/weft-base"]})
```

- An EnvID already realized under a read-only root is **adopted in
  place** — verified (marker, digest, activation), never written or
  leased. Zero user disk; `realize.adopted via=ro-root`.
- **Writable-first precedence**: your own healthy copy always wins over
  a read-only one (so a broken base never traps you).
- Your `extends_env` deltas **overlay over read-only parents** — the
  overlay only ever reads the parent.
- Lifecycle honesty: adopted bases are `read_only` in `env_status` /
  `site_footprint` (`evictable: false`); `env_evict` refuses (not yours);
  `env_repair` drops the adoption without touching the files. A base that
  fails integrity is REPORTED (`realize.ro_integrity_failed`, naming the
  owner's action) and weft builds a private copy in your root so work
  continues — set site policy `ro_integrity: "fail"` if governance
  demands stopping instead.
- Trust note: adoption verifies integrity, not provenance — you trust the
  read-only root as far as its filesystem permissions imply (same
  doctrine as `shared: true`). For provenance guarantees, distribute
  bundles (`bundle_export`/`bundle_import`) instead.
- For ADMINS: users' overlay children reference your base by fingerprint;
  evicting or rebuilding it degrades their overlays to private full
  prefixes at next use (loud, self-healing — but announce big rotations).
