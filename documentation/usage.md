# Weft — Usage

## Setup

Development happens through pixi (repo `pixi.toml`); the pixi binary lives
in `.env/bin` (gitignored). All commands below assume:

```sh
source .env/env.sh          # PATH + pixi cache locations under .env/
pixi run pytest -q -m "not solver and not docker"   # fast unit suite
pixi run pytest -q                                   # everything (network + docker)
```

Test markers: `solver` needs conda-forge access; `docker` needs the local
Docker daemon; `slow` marks long chaos tests.

## The tool surface in five minutes

```python
from weft.api import Weft

w = Weft(workspace="/path/to/project", pixi_bin=".env/bin/pixi")

# 1. register a site (user-confirmed action)
w.register_site("local", "local", {"root": "/path/site-root",
                                   "pixi_source": ".env/bin/pixi"})
w.sites_list()          # → [{name, kind, health, cpus, mem_gb, scheduler, …}]

# 2. describe an environment (declarative; solved+locked once, cached forever)
ensured = w.env_ensure({
    "name": "hep-fit",
    "deps": {"conda": ["python =3.12", "numpy", "scipy", "iminuit"]},
    "env_vars": {"OMP_NUM_THREADS": "{{cpus}}"},
})
env_id = ensured["env_id"]          # env:v1:…  (status: solved | cached)

# 3. register input data
ref = w.data_register("raw/run2189.csv")["ref"]     # dref:…

# 4. submit (returns immediately with a plan)
r = w.task_submit({
    "command": "python fit.py --data data/run.csv --out results/",
    "env": env_id,
    "inputs": [{"ref": ref, "mount_as": "data/run.csv"}],
    "code": {"ref": w.data_register("fit.py")["ref"], "mount_as": "fit.py"},
    "outputs": ["results/"],
    "resources": {"cpus": 8, "mem_gb": 16, "walltime": "01:00:00"},
    "site": "auto",                 # placement: ranked sites with reasons
    "label": "june fit, run 3",     # human handle in lists/events (≤200
                                    # chars); NOT identity: relabeling
                                    # never forks memoization
})
r["plan"]     # {"env": {"action": "cached"}, "staging": {"bytes_to_move": …}, …}

# 5. watch events instead of blocking
feed = w.events_poll(0)             # → job.state / job.staged / job.done events
w.task_status(r["job_id"])
w.task_logs(r["job_id"], tail=50)

# 6. results are manifests with previews; fetch bulk data only if needed
m = w.task_result(r["job_id"])      # outputs: [{path, ref, bytes, preview}]
w.data_fetch(m["outputs"][0]["ref"], "results/scan.h5")
```

### Task fields

| field | meaning |
|---|---|
| `command` | shell command run inside the activated environment |
| `env` | EnvID, inline spec dict, or `null` for the bare site environment |
| `inputs` | `[{ref, mount_as}]` — sandbox-relative mounts, read-only by convention. On one filesystem staging is zero-copy hardlinks (the mount, the CAS blob, and possibly the registered original share an inode) — mutating an input in place falsifies the content-addressed record, not just one file; tools that must mutate should copy inside the sandbox first |
| `code` | same shape; code is just data (hash-addressed like everything) |
| `outputs` | declared result paths — plain files (`plot.svg`) or directories (`results/`); a declared output that was not produced fails the job |
| `resources` | `cpus, mem_gb, gpus, walltime, partition` — validated against site capabilities AND user policy |
| `site` | site name or `"auto"` |
| `array` | N: fan out N element jobs with `WEFT_ARRAY_INDEX` = 0…N-1 |
| `env_vars` | exported in the job; `{{cpus}}`/`{{mem_gb}}`/`{{gpus}}` templated |
| `after` | job_ids that must be DONE first — pipelines without polling; a failed upstream fails this job as `task.dep_failed` (it never starts) |

Sandbox contract: the job's working directory contains its mounted inputs,
pre-created output dirs, and `tmp/`; guaranteed variables `WEFT_JOB_ID`,
`WEFT_CPUS`, `WEFT_MEM_GB`, `WEFT_GPUS` (+ `WEFT_ARRAY_INDEX` in arrays).

### Environment composition

```python
base = w.env_ensure({"name": "base", "deps": {"conda": ["python =3.12", "numpy"]}})
# layer on top: one line, whole-spec re-solve, new EnvID, shared package cache
from weft.spec import EnvSpec
parent_hash = EnvSpec.from_dict({...same base dict...}).spec_hash()
child = w.env_ensure({"extends": parent_hash, "deps": {"conda": ["emcee"]}})

# or freeze the base and add a package mid-analysis: extends_env pins the
# parent's ENTIRE resolution (incl. layer snapshot dates and github SHAs),
# so the child is a superset by construction — and realizes as an O(delta)
# overlay on the parent's prefix when the delta is pure language-layer
quick = w.env_ensure({"extends_env": base["env_id"],
                      "deps": {"pypi": ["emcee"]}})
quick["delta"]["layerable"]   # True → overlay fast path; else why-not text
```

`extends` lets the base move (full re-solve); `extends_env` never moves it
(a contradicting delta is `env.layer_conflict`, not a silent version
change). Overlay vs full prefix is a realization detail: same EnvID, same
results — held byte-identical by a conformance test.

Re-solving an unchanged spec never happens implicitly; pass
`update=True` to `env_ensure` to pick up new channel state (old EnvID
remains valid for reproducing past results).

### Failure handling (what an agent should do)

Every failure is `{"error": code, "stage", "detail", "hints", "retryable"}`.
Read the code, use the hints:

| code | recovery hint payload |
|---|---|
| `env.solve_conflict` | `solver_message`, `user_pins` → relax a pin, re-ensure |
| `site.capability_violation` | max per resource → right-size the ask |
| `job.oom` | `observed_peak_gb` vs `requested_gb` → resubmit bigger |
| `job.walltime_exceeded` | elapsed vs asked → raise walltime or shrink task |
| `job.nonzero_exit` | `log_signature` + `log_tail` → fix the actual bug |
| `data.verify_failed` | locations demoted → resubmit re-transfers |
| `env.unsatisfiable_on_site` | alternative sites → re-place |
| `env.platform_mismatch` | `locked_platforms` vs `site_platform` → add the site's platform to the spec, re-ensure (new EnvID) |
| `internal.error` | a weft bug, not a known failure mode — `hints.traceback_tail`; retry may not help, report it |

Never resubmit an unchanged failing task more than once (doctrine, doc 05 §7).

### Retaining run outputs (retain marks; storage moves only when it must)

Every finished run records an inventory of what it left behind
(`run_inventory` — knowledge that survives all cleanup). A KEEP is a
pinned selection at a durable address; where it lives follows from the
site's one storage fact, `durable`:

- `durable: true` (the root is safe): `run_retain` MARKS in place —
  zero bytes move, the sandbox paths stay valid forever, the sandbox
  becomes sweep-exempt. `{"moved": false}` says so.
- `durable: "/abs/path"`: one site-side hop into
  `<path>/runs/<label>/<target>/` — never crosses the wire.
- neither: the site can keep nothing — pass `dest="@workspace"`
  (background transfer home, progress events) or the call refuses
  with `retain.no_durable` and the three levers in its hints.

`run_forget` is the INVERSE of retain: it removes what retain created
(the pin always; copies only where a move made them) — unmarking
deletes nothing. `run_discard` alone destroys sandbox bytes, and on a
still-marked target it is SELECTIVE (junk goes, keeps stay); full
deletion is forget then discard. The TTL sweep
(`policy: {run_remains_days: N}`) is opt-in and defaults to OFF;
retained targets are exempt regardless.

Files are addressed by the (run, relpath) KEY everywhere:
`run_file_stat/read(target, rel)` resolve sandbox → keep and say which
answered (`at`); task inputs accept `{"run": ..., "rel": ...,
"mount_as": ...}` (resolved to the output's ref — no rehash for
declared outputs); `data_register(run=, rel=)` re-enters explicitly.
Keeps of declared outputs anchor their refs: after cache eviction,
fetch and staging re-obtain the bytes from the keep, hash-verified.

### Reference-in-place (big data on stable storage)

`data_register(path, site=..., ingest=False)` hashes a site path
without copying it: the path is recorded as the ref's durable home,
same-site tasks mount it as a symlink (zero bytes move; read-only
inputs contract), and a stat-fence at every staging fails
`data.verify_failed` — naming the external source — if the home
drifted. Bytes ingest lazily only when they must move off-site
(`data.ingested_for_transfer`). GC never touches external homes.
`data_fingerprint(path, site)` gives the cheap stat manifest
(`hash_under=` samples small files) for registration-time fingerprints
and drift detection without minting identity.

### Session lifecycle

Sessions track `last_used` (every session verb touches it);
`list_sessions(site)` reports `idle_s` and `has_kernel` per session,
and an `env_evict` blocked by active sessions lists the same facts per
holder. A record whose directory is gone (crash leftover) is retired
by `gc_orphans` — it could never serve an exec and would block evict
forever. Site policy `session_idle_days` (default OFF) lets the gc
sweep stop kernel-less sessions idle past the threshold; sessions with
a running kernel are never touched, and without the policy nothing
reaps automatically — `session_stop` remains the contract.

### Controller on a submit node

Registering a `slurm` site without `host` (or with `transport:
"local"`) runs every scheduler call and file operation as a direct
subprocess — for controllers that live on the cluster's login/submit
node, where ssh-to-self is often impossible (GSSAPI/Kerberos-only).
Staging becomes local-link on the shared filesystem.

### Published environments (institutional read-only bases)

```python
# admin: build a squashfs image AT the shared tree + catalog it by name
w.env_publish(env_id, "hpc", "/groups/lab/weft-base",
              name="lab-py", version="2026.07")
# consumer: adopt by NAME from the catalog's stored lock — no solving
env = w.env_adopt("hpc", "/groups/lab/weft-base", "lab-py")["env_id"]
mine = w.env_ensure({"extends_env": env, "platforms": ["linux-64"],
                     "deps": {"pypi": ["emcee"]}, "name": "mine"})
w.env_published("hpc", "/groups/lab/weft-base")     # what is offered
w.env_unpublish("hpc", tree, "lab-py", "2026.07")   # pointer only;
                                                    # purge=True deletes
```

The tree must live OUTSIDE any weft root; publish is a rebuild FOR the
destination path (baked absolute paths) and is audited as "user".
Versions are catalog pointers over immutable content-addressed dirs —
upgrades publish alongside and flip `latest`, never edit in place. The
base is filesystem-read-only for consumers (EROFS), adopted in place
via ro_roots, mounted per-job in private namespaces where userns
exists — and `extends_env` overlays stack on top exactly as on private
parents.

On userns sites the build's file churn does NOT hit the tree: the
prefix materializes in a staging dir bind-mounted at the tree path
inside each build command's namespace, and the tree receives one
sequential `image.sqfs` write — decisive when the tree is slow netfs
(NFS metadata ops are the pathology; ~10^4 small files become one
stream). `staging=` on `env_publish` ('auto' default → under the site
root; an absolute dir, e.g. node-local or parallel scratch; 'none' for
the classic build-at-destination), or site config `publish_staging` for
the site's default. A live probe gates it — where the bind cannot work
the build falls back to the destination and says so (`staging` field in
the result; `realize.staged` / `realize.staging_skipped` events).
Consumers are unaffected either way: same image, same mount path.

### Data between sites

Routes are probed at registration (`site_route_probe(src, dst)` re-probes):
a shared filesystem or a direct dst→src ssh path (your own keys — weft
stores none). Staging then links/pulls site-to-site with the controller
detour as fallback; the submit plan (`staging.site_to_site`) and
`transfer.done via=...` events show which route each ref took. Sites
behind NAT/port maps set `peer_host`/`peer_port`.

### Remote sites

```python
# SSH workstation (uses your ~/.ssh config; nothing stored by weft)
w.register_site("beamlab", "ssh", {
    "host": "beamlab", "root": "/data/$USER/.weft",
    "pixi_source": ".env/bin/pixi",     # pushed once, hash-verified
})
# pixi_source is optional; registration checks bin/pixi RUNS on the site
# and otherwise fetches the release pinned in weft.site_tools for the
# site's own platform (cross-platform controllers just work; cache:
# ~/.cache/weft/site-tools, override versions via WEFT_PIXI_VERSION)
#
# registration narrates progress as bootstrap.step events (bootstrap →
# probe → tools → routes). probe_only=True bootstraps + probes and
# registers NOTHING (check-before-commit; the shim — ~100KB — is still
# written under the root: a real probe needs it).
#
# quirk levers (agents fix sites without weft code changes):
#   scheduler.extra_directives: ["--constraint=ib"]  raw #SBATCH lines,
#     validated (weft-managed + identity flags refused, structured
#     lever named); per-task: resources.scheduler_directives
#   site_prelude: "module purge"   shell before EVERY job's activation
#   capabilities_override / modules_init / prefer / policy: as before
# site_unregister(name) forgets a registration without touching the
# site (refuses while work is live there; re-registering re-adopts
# realized envs and staged data). site_teardown remains the cloud
# instance killer.

# host reachable only from inside (bastion → target): model the hops.
# weft renders nested ProxyCommand chains (your keys/options apply at
# EVERY hop, which plain -J does not do), multiplexes the connection,
# self-heals a wedged multiplexer after a hop restart, and `doctor`
# reports which hop died ("chain breaks at me@bastion")
w.register_site("inner", "ssh", {
    "host": "node7.internal", "root": "/data/me/.weft",
    "jump": ["me@bastion.univ.edu"],
    "pixi_source": ".env/bin/pixi",
})

# Slurm cluster through its login node. ro_roots: admin-owned base envs
# are ADOPTED in place (read-only, verified, zero user disk); your own
# builds and extends_env overlays land in your root
w.register_site("hpc", "slurm", {
    "host": "login.hpc.example.edu", "root": "/scratch/me/.weft",
    "ro_roots": ["/opt/team/weft-base"],
    "pixi_source": ".env/bin/pixi",
    "scheduler": {"account": "phys-lab", "partition": None},
    "modules_init": "export MODULEPATH=/opt/site-modules",  # site quirk knob
    "policy": {                                # user rules, enforced+surfaced
        "partitions_allowed": ["standard", "short"],
        "max_gpus": 4,
        "max_concurrent_jobs": 50,
        "storage": {"large": "/groups/phys/me", "scratch": "/scratch/me",
                    "node_tmp": "/tmp"},
        "notes": ["prefer nights/weekends for >1h jobs"],
    },
})
w.module_check("hpc", ["espresso/7.2"])   # lazy module inventory

# Cloud (provisioner-backed, hard budget caps)
w.register_site("cloud-gpu", "cloud", {
    "provisioner": "skypilot",
    "budget": {"max_usd": 20, "max_hours": 2},   # refused if estimate exceeds
    "resources": {"cpus": 8, "mem_gb": 32,
                  "gpus": [{"model": "A100-40GB", "count": 1}],
                  "cuda_driver": "12.4"},
})
w.env_gpu_hint("cloud-gpu")   # what cuda-version to pin for this site
w.site_teardown("cloud-gpu")  # explicit; watchdog also tears down on overrun
```

Session environments (interactive exploration, doc 03 §7):

```python
s = w.session_start(env_id, "beamlab")           # LAZY: no clone yet —
                                                 # runs from the base
                                                 # realization in place
w.session_exec(s["session_id"], "python -c 'import emcee'")   # probe
w.session_install(s["session_id"], conda=["emcee"])           # FIRST mutation
                                                 # clones the prefix (seconds
                                                 # against a warm cache)
snap = w.session_snapshot(s["session_id"])       # minimal delta → real EnvID
# re-run the final computation under snap["env_id"] → enters provenance
```

A session buys mutability, and the writable clone is its price — paid at
the first `session_install`/`run_installer`, not at start. A no-additions
session never lays down a per-session prefix (on BeeGFS/Lustre that's a
~10^5-file hardlink forest defeating the very squashfs mount it shadows);
its snapshot short-circuits to the base EnvID. Python kernels attached
before the first install still see installed packages live on their next
block (the driver holds the future prefix on `sys.path` — the forward
hook); R/julia kernels attached pre-install need `kernel_restart`, and
the install result says so. If you never intend to install,
`kernel_start(site, env_id=...)` attaches to the realization directly
and needs no session at all.

On an **adopted/imported base** — one that arrived as a read-only pack
or an unpacked archive rather than being *built* on this site (the
record calls this a cold base: the site's package cache holds none of
its packages, a fact fixed at adoption and never re-probed) — cloning
the manifest would re-download the entire base from the index (1.6 GB
in the field case; impossible on an egress-restricted node). So there,
pypi adds materialize a **pylib overlay** instead: the delta is
resolved *with the base visible* (`pip --dry-run --report`), only the
missing closure is fetched (`--no-deps --target`), and the layer
composes over the mount via `PYTHONPATH` (persisted in the session's
`overlay.sh`; `runtime` carries `pylib` and the composed activation).
conda adds and bespoke installers there refuse with `session.cold_base`
and three levers: `extends_env` (mint a real delta env — the citable
twin of the same composition), run it where the base was *built* (warm
cache), or `full_clone=true` (fetch the whole base; needs egress). The
mode is decided once from the base's provenance and recorded on the
session — every later install takes the SAME lane (each resolve sees
base + the existing layer), and mode mixing is refused, so mechanisms
never switch or clash mid-session. On built-here bases nothing changes.

**R is first-class**: `session_install(cran=[...])` composes a session
`rlib` over the base via `R_LIBS` on ANY base, frozen or built-here —
R's installer checks every `.libPaths()` entry and skips base-satisfied
deps natively, so it is delta-only with no clone and no two-phase dance.
Running R kernels see the package on their next `library()` call
(driver hook). The session-on-a-frozen-base cost map:

| add    | frozen (adopted) base | mechanism |
|--------|----------------------|-----------|
| `pypi` | delta-only           | pylib layer, `PYTHONPATH` |
| `cran` | delta-only           | rlib layer, `R_LIBS` |
| `conda`| refuse + levers      | cannot layer (embedded prefixes) |

The snapshot carries all three (`deps.cran` in the minted spec), and
because `classify_delta` layers cran, the citable env ALSO realizes as
a delta overlay on the frozen base — scratch and snapshot agree.

Callers that exec interpreters themselves consume the **runtime
contract** instead of rederiving prefix layouts: `session_runtime(id)`
(also on `session_start`/`session_install` results — the install echo
is the flip moment — and on `list_sessions` rows) returns `{source:
session|base, env_id (null once mutated — scratch has no identity),
prefix, activation, ns_wrap, direct_exec}`. `activation` is always
correct; `direct_exec` says when `prefix/bin/*` may be exec'd without
it — a squashfs base's prefix is mount-scoped and only exists under
activation (`ns_wrap` ⇒ inside `unshare -rm`). Runtime queries are
observation, not activity: they don't touch `last_used`.

### Monitoring, arrays, load

```python
w.site_load("hpc")                          # idle CPUs+GPUs per partition,
                                            # backlog, QOS, my associations
w.site_load("hpc", resources={"cpus": 8, "walltime": "04:00:00"})
                                            # + sbatch --test-only start ETA
w.site_load("hpc", resources={"gpus": 2}, partitions=["gpu", "short"])
                                            # ETA per candidate partition
w.site_associations("hpc")                  # MY accounts/QOS ceilings/fairshare
w.module_list("hpc", search="cuda")         # discover site software offerings
r = w.task_submit({..., "array": 2000})     # fan-out with WEFT_ARRAY_INDEX
w.events_poll(cursor)                       # compact: array digests, transfer
                                            # progress, job states (non-array)
```

**Events contract** (for reducers/consumers): every event is
`{"seq", "kind", "job_id", ...payload}` — `job_id` is a first-class
column on the row (often null for non-job events), NOT a payload key.
Terminal job transitions arrive as THREE kinds, not one: `job.done`,
`job.failed` (payloads differ — manifest summary vs error dict), and
CANCELLED as `job.state` with `state="CANCELLED"`. There is no
`job.state` with DONE/FAILED. Lease deaths are `kernel.died` /
`service.exited`, each carrying `cause`
("walltime_exceeded"/"oom"/"cancelled"/"exited"/"lost") and, on
scheduler sites, the raw `slurm_state`. Unknown kinds should be
ignored (new kinds are always additive).

```python
w.array_status(r["group"])                  # counts + FAILURE BUCKETS (by
                                            # log signature, sample indices)
w.array_elements(r["group"], state="FAILED", limit=50)   # page big sweeps
w.array_retry(r["group"])                   # linked retries; digests heal
                                            # (replaced rows carry
                                            # superseded_by — fold, don't
                                            # re-count them)
w.array_result(r["group"])                  # roll-up: wall stats, failures
w.env_repair(env_id, "hpc")                 # clear a corrupt realization

w.jobs_where(state="FAILED", limit=50)      # enumerate: jobs …
w.list_envs(); w.list_kernels(); w.list_services()   # … and everything else
w.audit_tail(50)                            # one trail, user + agent
w.task_status(job_id)[0]["plan"]            # the submit-time promise,
                                            # persisted (survives restarts;
                                            # arrays store one group plan)
```

Partition records carry `gres` (GPU model/count) and `features`; GPU asks
validate against them (login nodes have no GPUs), and refusals name the
fitting partitions.

Off-CI regression scenarios live in `misc/scenarios/scenarios.py`
(gitignored): 21 end-to-end runs against dockerized sites —
`pixi run python misc/scenarios/scenarios.py`.

### Multi-ecosystem environments (R/CRAN/GitHub, more to come)

R specs can widen the repository universe beyond the dated base mirror:
`r_repositories` (extra CRAN-like repos, resolved jointly for the closure)
and `r_release_repos` (`{provider, release}` — a registered provider
expands a named release line to its repo set + required R version,
validated against the conda layer). Both are identity: they change what
resolves, so they change the EnvID; packed/air-gapped delivery and
`extends_env` overlays compose unchanged.


```python
env = w.env_ensure({
    "name": "r-analysis",
    "deps": {"conda": ["r-base =4.4"],                  # interpreter layer
             "cran": ["data.table",                     # snapshot-locked
                      "jsonlite ==2.0.1",               # exact assertion
                      "lab/pkg@fix-branch"]},           # github → pinned SHA
    "system_requirements": {"cran_snapshot": "2026-07-01"},  # frozen forever
})
env["layers"]                        # per-layer package counts, source builds
w.env_ensure(spec, dry_run=True)     # test a fix; nothing stored
w.env_why(env_id, "data.table")      # what pulls it in / the locked record
```

Missing interpreter → `env.layer_conflict` names exactly what to add.
Unknown deps key → the registered-solver list. Adding an ecosystem =
one Solver class + one registry entry (`solvers.default_solvers`).

### Kernels (incremental interactive execution)

```python
k = w.kernel_start("beamlab", "python", env_id=env_id)["kernel_id"]
w.kernel_exec(k, "grid = load_grid()")            # state persists
r = w.kernel_exec(k, "fit = slow_scan(grid)", wait=False)   # async block
w.kernel_poll(k, r["block"], timeout=30)          # watch it
w.kernel_interrupt(k)                             # hung block → rc 130
w.kernel_transcript(k)                            # what ran, in order
# native crash → kernel.died event names the killing block; then:
w.kernel_restart(k, replay="successful")          # state rebuilt
w.kernel_stop(k)
```

Exploration only: assemble the successful blocks into a script and run it
as a normal task for the citable record.

### Provenance

```python
w.provenance(job_id)     # command + env identity + inputs, recursively
w.provenance("dref:…")   # who produced this artifact, all the way down
```

### Diagnostics

```python
w.doctor()                                  # shim health per site, stale jobs
                                            # (multi-hop sites: which hop died)
w.site_exec("local", "df -h .", why="check quota before big staging")
w.job_node_exec(job_id, "nvidia-smi; free -m",
                why="job looks stuck")      # INSIDE the job's allocation
w.site_probe_deep("hpc", partitions=["gpu"])  # compute-node truth via
                                            # probe jobs (measured egress)
w.audit_tail(50)                            # what ran where, and why
w.reconcile()                               # after a controller crash/restart
```

The trail's actor is set by the EMBEDDER at construction
(`Weft(default_actor="user")` for a UI serving a human; default
"agent") — never per call, so nobody can write someone else's name.
Registration-class actions (`register_site`, `site_unregister`,
`site_teardown`) always audit as "user": they are user-confirmed by
doctrine.

### MCP server

```sh
python -m weft.mcp_server --workspace /path/to/project \
    --pixi-bin .env/bin/pixi      # stdio JSON-RPC; tools/list has schemas
```
Contract: every tool returns JSON; failures are structured error payloads
flagged `isError` — nothing raises across the boundary.

### Julia environments

```python
w.env_ensure({"name": "jl", "deps": {"conda": ["julia"],
                                     "julia": ["Example"]}})
# Manifest.toml-locked (content tree-hashes); github: "owner/Repo.jl@ref"
```

### Housekeeping

```python
w.gc_plan()                      # reclaimable bytes per site (dry)
w.gc_sweep("hpc", confirm=True)  # explicit; content rebuilds on next use
w.env_evict(env_id, "hpc")       # reclaim a prefix; rebuild is seconds
w.env_evict(parent, "hpc", cascade=True)   # take overlay children with it
w.gc_events(older_than_days=30)
w.task_logs(job_id, follow_cursor=0)   # live log following
```

Eviction refuses (`env.evict_blocked`) while queued/running jobs, open
sessions/kernels, or realized overlay children depend on the env — the
hints name them and the lever. GC recency is *usage* (`last_used`), not
state age, sweeps go through the same guarded evict path, and orphan
scans never touch dirs that carry a valid env marker, a fresh lease, or
recent writes (other users' work on shared roots is out of scope by
construction).

### Services (endpoint-publishing processes)

```python
r = w.service_start("hpc", {"command": "python app.py --port $WEFT_PORT",
                            "env": env_id,
                            "inputs": [{"ref": ref, "mount_as": "d/run.h5"}],
                            "outputs": ["logs/"]},
                    ports=[8501])
r["endpoints"][0]["url"]          # tunneled back to the controller
w.service_stop(r["service_id"], collect=True)
```
Loopback-bound on the site; the SSH tunnel is the auth boundary (Slurm:
hops login→compute node). `service.ready` / `service.exited` in the feed.

### Remote data ingest, promotion, shared sites

```python
w.data_register("https://example.org/run.h5", site="hpc")   # into site CAS
w.kernel_promote(k, blocks=[7])        # transcript-grade manifest
w.register_site("hpc", "slurm", {..., "shared": True})      # team caches
```

### Adaptivity: forgiving solves, drift, reclamation

```python
# one call instead of a conflict-relax-retry loop ('?' = soft constraint)
w.env_ensure({"deps": {"conda": ["python =3.12", "scipy ==1.14.1?"]}},
             relax="soft")        # → {"relaxed": [...]}; result still pinned

# explore cheaply; capture the bespoke fix; snapshot it with your reasoning
s = w.session_start({"deps": {"conda": ["python =3.12"]}}, "beamlab")
w.session_run_installer(s["session_id"], "pip install ./vendored",
                        note="upstream wheel broken on this platform")
w.session_snapshot(s["session_id"], notes=["drop when upstream 2.2 ships"])

# the world moved: revise instead of dead-ending (or site policy on_drift)
w.env_revise(env_id)              # → new EnvID + package-level diff
w.env_find_near(spec, site="hpc") # warm near-matches, with their diffs

# reclaim disk without losing the way back
w.site_footprint("hpc")           # prefixes vs shared cache vs data
w.env_evict(env_id, "hpc")        # rebuild = seconds, offline (cache warm)
```

Every env and manifest carries a **reproducibility grade** (`fully-pinned`
→ `snapshot-pinned` → `attested` → `escape-hatch` → `state-dependent`) plus
the per-component breakdown, and identity-neutral `notes` / `step_notes`
recording *why* an adaptive step was taken. weft grades and reports; the
agent decides.
