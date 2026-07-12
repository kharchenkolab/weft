# weft

Execution substrate for agent-driven scientific analysis: run compute
tasks on **your** machines — the laptop, an SSH workstation, a Slurm
cluster, a cloud burst — with reproducible environments, content-addressed
data, and a compact asynchronous API designed for LLM agents.

*Design codename "Fabric": the full design rationale lives in [`docs/`](docs/);
what is actually built (and where it deviates) in
[`documentation/`](documentation/); an agent-facing operating guide in
[`skills/weft/`](skills/weft/SKILL.md).*

## What it does

- **Declarative environments, multiple ecosystems.** A spec (conda/PyPI
  deps — plus CRAN and GitHub R packages locked against dated snapshots
  with SHA-pinned refs; more solvers plug into a registry — with site
  modules and CUDA pins) solves once into a locked, content-addressed
  **EnvID**; realization on each site is automatic — `pixi install` where
  there's network, pre-packed archives for air-gapped compute nodes, site
  `module load`s layered in. Identical resolutions are never rebuilt,
  anywhere. Everything is userspace: no root, no docker, no daemons on
  remote machines — one inspectable directory, removable in one command.
- **Content-addressed data.** Inputs stage to a site at most once; outputs
  chain site-side (a dependent task moves 0 bytes); results come back as
  manifests with previews, bulk data only on request; identical tasks
  memoize. Every result traces back to exact command + locked env +
  input hashes.
- **Sites as data.** Capability probing (arch, glibc, scheduler,
  partitions, GPUs, runtimes), live load (`site_load`: idle CPUs, queue
  backlog, GPU utilization, scheduler start ETAs), per-site user policy
  (partition allowlists, GPU caps, storage roles, free-form guidance),
  and placement that weighs cache warmth and current load — with reasons.
- **Interactive when you need it.** Persistent kernels (Python/R/Julia)
  execute code blocks incrementally with live interpreter state — over
  plain SSH, no sockets — with interrupt, crash diagnosis (which block
  killed it), and transcript replay; sessions and snapshots turn
  exploration into locked, citable environments; `provenance()` walks any
  result back to exact commands, locked envs, and input hashes.
- **Built for agents.** Structured errors with remediation hints
  (`job.oom` carries observed peak vs asked; queue reasons name why a job
  pends), plans before effects, coalesced event digests for 1000-element
  arrays with one-call retry of failed elements, live load/queue/GPU
  status with scheduler start ETAs, an audited diagnostic shell, and
  crash/outage semantics you can rely on (controller crashes reconcile;
  remote reboots are detected; connectivity loss never fails a detached
  job). Cloud spend sits behind hard budget caps with a runaway watchdog.

## Quick look

```python
from weft.api import Weft

w = Weft("~/analysis-2189")
w.register_site("hpc", "slurm", {"host": "login.hpc.edu",
                                 "root": "/scratch/me/.weft",
                                 "pixi_source": "bin/pixi"})
env = w.env_ensure({"name": "fit", "deps": {"conda":
        ["python =3.12", "numpy", "iminuit"]}})["env_id"]
r = w.task_submit({
    "command": "python scan.py --out results/",
    "env": env,
    "code": {"ref": w.data_register("scan.py")["ref"], "mount_as": "scan.py"},
    "outputs": ["results/"],
    "resources": {"cpus": 8, "walltime": "02:00:00"},
    "site": "hpc", "array": 500,
})
# ... events: QUEUED → RUNNING → array.progress digests → array.done
```

## Development

```sh
source .env/env.sh                                   # local pixi toolchain
pixi run pytest -q -m "not solver and not docker"    # fast unit lane
pixi run pytest -q                                   # full matrix: needs
                                                     # docker + network
python misc/scenarios/scenarios.py                   # off-CI e2e scenarios
```

Integration tests run against dockerized fixtures: an SSH workstation, a
single-node Slurm cluster (with a mock site-module), hostile boxes (old
glibc, toolless, musl), and a mock cloud provisioner. Status: phases 0–3
of the roadmap implemented; see `documentation/architecture.md` for the
deviations log and current limits.
