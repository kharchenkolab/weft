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

### Diagnostics

```python
w.doctor()                                  # shim health per site, stale jobs
w.site_exec("local", "df -h .", why="check quota before big staging")
w.store.audit_tail(50)                      # what ran where, and why
w.reconcile()                               # after a controller crash/restart
```
