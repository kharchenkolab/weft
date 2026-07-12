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
| `inputs` | `[{ref, mount_as}]` — sandbox-relative mounts, read-only by convention |
| `code` | same shape; code is just data (hash-addressed like everything) |
| `outputs` | declared result paths; a missing declared output fails the job |
| `resources` | `cpus, mem_gb, gpus, walltime` — validated against site capabilities |
| `site` | site name or `"auto"` |
| `array` | N: fan out N element jobs with `WEFT_ARRAY_INDEX` = 0…N-1 |
| `env_vars` | exported in the job; `{{cpus}}`/`{{mem_gb}}`/`{{gpus}}` templated |

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
```

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

Never resubmit an unchanged failing task more than once (doctrine, doc 05 §7).

### Remote sites

```python
# SSH workstation (uses your ~/.ssh config; nothing stored by weft)
w.register_site("beamlab", "ssh", {
    "host": "beamlab", "root": "/data/$USER/.weft",
    "pixi_source": ".env/bin/pixi",     # pushed once, hash-verified
})

# Slurm cluster through its login node
w.register_site("hpc", "slurm", {
    "host": "login.hpc.example.edu", "root": "/scratch/me/.weft",
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
s = w.session_start(env_id, "beamlab")           # scratch clone, unhashed
w.session_exec(s["session_id"], "python -c 'import emcee'")   # probe
w.session_install(s["session_id"], conda=["emcee"])           # seconds (cache)
snap = w.session_snapshot(s["session_id"])       # minimal delta → real EnvID
# re-run the final computation under snap["env_id"] → enters provenance
```

### Monitoring, arrays, load

```python
w.site_load("hpc")                          # idle CPUs, backlog, GPU util, QOS
w.site_load("hpc", resources={"cpus": 8, "walltime": "04:00:00"})
                                            # + sbatch --test-only start ETA
r = w.task_submit({..., "array": 2000})     # fan-out with WEFT_ARRAY_INDEX
w.events_poll(cursor)                       # compact: array digests, transfer
                                            # progress, job states (non-array)
w.array_status(r["group"])                  # counts + failure previews
w.array_result(r["group"])                  # roll-up: wall stats, failures
w.env_repair(env_id, "hpc")                 # clear a corrupt realization
```

Off-CI regression scenarios live in `misc/scenarios/scenarios.py`
(gitignored): 12 end-to-end runs against dockerized sites —
`pixi run python misc/scenarios/scenarios.py`.

### Multi-ecosystem environments (R/CRAN/GitHub, more to come)

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
w.site_exec("local", "df -h .", why="check quota before big staging")
w.store.audit_tail(50)                      # what ran where, and why
w.reconcile()                               # after a controller crash/restart
```
