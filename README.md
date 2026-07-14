# weft

A Python library for managing software environments and running compute
jobs on your machines — your laptop, a lab workstation, an HPC cluster —
through one small API.

Point weft at a server you can ssh into. It figures out what that server
is and how it is configured — scheduler and partitions, GPUs, installed
module system, network access, storage — whether it's a single node or a
large cluster. From then on you (or your AI agent) can build environments
and run jobs there, and weft handles the plumbing: shipping inputs,
building the environment where the job runs, queueing, watching, and
bringing results back with a record of exactly how they were made.

Weft is a backend library designed to be driven by code or by an LLM
agent. For a point-and-click way in, see
[weft-ui](https://github.com/kharchenkolab/weft-ui) — a reference web UI
built on weft.

## Installing

```sh
git clone https://github.com/kharchenkolab/weft && cd weft
pip install -e .
```

You also need the [pixi](https://pixi.sh) binary on your own machine
(`brew install pixi`, or the one-line installer from pixi.sh). That's
all: remote machines need nothing pre-installed — weft provisions them
itself, entirely in userspace.

## A first session

```python
from weft.api import Weft

w = Weft("~/my-analysis")     # a workspace; all weft state lives in it

# 1. Point weft at a machine. One ssh hostname is enough — weft probes
#    the rest (no root, no daemons; it keeps everything under one
#    directory that you can delete to remove all traces).
w.register_site("cluster", "slurm", {
    "host": "login.hpc.example.edu",   # ~/.ssh/config aliases work too
    "root": "/scratch/me/.weft",       # where weft may keep its files
})

w.sites_describe("cluster")
# discovered by probing — not typed in by you:
#   scheduler: slurm 24.05, partitions: cpu (64 nodes), gpu (8 × A100)
#   nodes: 128 cores, 512 GB, glibc 2.28, internet from compute: yes
#   modules: Lmod (2000+ available)   storage: /scratch 12 TB free ...

# 2. Say what software you need. Weft resolves it into a locked,
#    reproducible environment and builds it on the cluster.
env = w.env_ensure({
    "name": "fitting",
    "deps": {"conda": ["python =3.12", "numpy", "scipy", "emcee"]},
})["env_id"]

# 3. Run work. Inputs are shipped once, the job is queued and watched,
#    results come back as previews with full provenance.
run = w.task_submit({
    "command": "python fit.py --out results/",
    "code": {"ref": w.data_register("fit.py")["ref"],   # ship your script
             "mount_as": "fit.py"},
    "env": env,
    "outputs": ["results/"],
    "resources": {"cpus": 8, "walltime": "02:00:00"},
    "site": "cluster",
})

w.task_status(run["job_id"])   # QUEUED → RUNNING → DONE (async: nothing
                               # blocks; poll status or subscribe to events)
```

The same code works when `"cluster"` is your laptop (`"local"`), a plain
ssh box, a Slurm cluster behind a bastion host, or a cloud instance —
weft picks the right transport, build strategy, and scheduler dialect
per site.

## What you get

- **Environments that are reproducible without effort.** Declare
  conda/PyPI packages — and R packages from CRAN or GitHub, pinned to
  dated snapshots so they resolve the same way next year. Weft locks the
  full resolution once and rebuilds exactly those versions on any
  machine; identical environments are never built twice. GPU stacks (CUDA
  pytorch/JAX) and cluster `module load`s are part of the same
  declaration.

- **Data that moves once.** Inputs are hashed and staged to a site at
  most once — a rerun or a dependent task ships zero bytes. Outputs come
  back as light previews (full files only on request), and every result
  can be traced back to the exact command, environment, and input data
  that produced it.

- **Machines you can ask questions about.** `sites_describe` shows what
  a machine is; `site_load` shows what it's doing right now (idle CPUs,
  queue backlog, GPU use, when a job would likely start); deep probes
  run on the compute nodes themselves, so decisions rest on measured
  facts rather than login-node guesses.

- **Three ways to run, one model.** Batch *tasks* (including
  1000-element array sweeps with one-call retry of failures);
  interactive *kernels* — a live Python/R/Julia session on a compute
  node, over plain ssh; and *services* — dashboards or notebook servers
  running next to the data, reachable through a private tunnel.

- **Team environments.** Build a heavy environment once and publish it
  to a shared read-only tree with a human name ("lab-py @ 2026.07").
  Colleagues adopt it by name — no rebuilding, no drift — and can layer
  their own extra packages on top without touching the base.

- **Built for agents, honest by design.** Failures are structured, with
  the cause and a concrete fix (an out-of-memory kill reports observed
  peak vs. requested and what to resubmit with; a pending job names the
  scheduler's reason). Site quirks are fixable from the outside —
  overrides for probing, scheduling flags, and per-site setup lines —
  so a capable agent can adapt to a strange cluster without patching
  weft. Cloud use sits behind hard budget caps.

- **Nothing to install on the remote.** No root, no containers
  required, no daemons: weft places a small POSIX script and static
  binaries under one directory of your choosing. Deleting that
  directory removes weft from the machine completely.

## Learn more

- [`documentation/`](documentation/) — architecture, usage guide, and
  schemas for what is actually built.
- [`skills/weft/`](skills/weft/SKILL.md) — the operating guide an LLM
  agent loads to drive weft well.
- [`docs/`](docs/) — the original design rationale.

Weft is young: the API is settling, and it is exercised continuously
against real Slurm clusters as well as a containerized test fleet.
