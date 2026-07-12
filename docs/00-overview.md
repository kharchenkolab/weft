# Fabric — Execution Substrate for Agent-Driven Physics Analysis

**Document 00 — Overview and Scope**
*Status: draft proposal · Working codename: "Fabric" (rename freely)*

## 1. Motivation

The host application is a desktop web app that orchestrates physics data analysis — spectral fits, event selection and histogramming, image reduction and stacking, parameter scans, Monte Carlo studies, small-to-medium numerical simulations — with an LLM agent at the center. The agent plans analyses, writes and runs code, and interprets results in dialogue with the user.

Today the app runs everything locally: a set of commonly used tools is pre-installed, and additional packages are layered on demand, with the option of building a fully custom environment when needed. This works on a single machine but breaks down as soon as a compute step outgrows the desktop — a likelihood scan over thousands of grid points, a detector-response simulation, a GPU-accelerated fit, or a reduction pass over a terabyte of instrument data. Users typically already have access to heavier resources: a lab workstation reachable over SSH, a departmental or facility Slurm cluster, or cloud accounts. The app should be able to use them.

Doing so raises three coupled problems. First, execution itself differs across sites: a direct process on a workstation, a batch job on a cluster, a provisioned instance in the cloud. Second — and hardest — every task needs a software environment, and the mechanism for materializing an environment differs per site: on your own workstation you can install anything; on a cluster you usually have no root, no Docker, sometimes no internet on compute nodes, but you do have Apptainer and a module system; in the cloud you build images. Third, inputs and outputs must move between sites without the user (or the agent) babysitting transfers, and without re-transferring the same gigabytes repeatedly.

Fabric is the proposed answer: a self-contained module that presents the agent with a small, uniform task-execution API and hides site heterogeneity, environment realization, and data staging behind it.

## 2. Design goals

The module should make the following statement true from the agent's point of view: *"I can request that a command run in a described environment against described inputs, on whichever registered site fits, and receive results — without knowing how any of that happens."*

Concretely, Fabric aims to provide a declarative environment model in which an environment is a specification, not a sequence of manual install steps, so the same spec can be realized on a macOS laptop, a Linux workstation, a Slurm cluster, and a cloud VM. It aims for aggressive reuse: environments and data are content-addressed, so anything already materialized on a site is never rebuilt or re-transferred. It aims to be root-free and admin-free on remote sites, because most users cannot install system software on shared machines. It aims to be asynchronous by default, because cluster queue waits are measured in minutes to hours and the agent must remain responsive. And it aims to be thin: wherever a mature tool already solves a sub-problem (SkyPilot for cloud provisioning, pixi for environment solving, rsync/rclone/Globus for transfer), Fabric wraps it rather than re-implementing it.

## 3. Non-goals

Fabric is not a workflow manager. It executes individual tasks; DAG logic, retry policy, and analysis planning live above it, in the agent. It is not a scheduler: it submits to existing schedulers and queues but does not arbitrate between users. It is not a data catalog or archive; it tracks the data it moves for caching and provenance, but long-term data management remains the user's domain. It does not attempt multi-tenant service deployment — the unit of trust is one user driving their own accounts and credentials. Finally, the domain scope is generic numerical and data analysis; nothing in the design depends on any particular physics subfield's semantics.

## 4. Representative scenarios

**S1 — Workstation offload.** A user analyzes accelerator beam-position data on a laptop. The agent proposes a bootstrap resampling study too slow for the laptop. The user has a 64-core lab workstation reachable as `ssh beamlab`. The agent requests execution on that site; Fabric bootstraps itself over SSH (one static binary), realizes the required Python environment from a lockfile (cached for next time), rsyncs the 2 GB input file (cached by hash), runs the job, and returns a result manifest with a preview table. Subsequent runs with the same environment and data start in seconds.

**S2 — Cluster batch scan.** The same project later needs a 2 000-point likelihood scan. The user's university cluster runs Slurm; compute nodes have no internet. Fabric realizes the environment on the cluster either by pushing a packed archive of the already-solved environment or by building an Apptainer image, stages inputs to scratch via the login node, submits an array job, and streams status back. The agent monitors asynchronously and summarizes partial results as they land.

**S3 — Cloud burst with GPUs.** A GPU-accelerated fit needs an A100 for an hour and no local option exists. Fabric delegates provisioning to a SkyPilot-backed site adapter, realizes the environment from the same spec (now as a container image), runs, retrieves outputs to the project workspace, and tears the instance down.

**S4 — Site-licensed software.** A materials-physics user needs a plane-wave DFT code available on the cluster only as an environment module. The environment spec declares `modules: ["espresso/7.2"]` alongside conda dependencies; on the cluster the realization includes the module load, while the local adapter reports the spec as unsatisfiable there — correctly steering the agent (and the user) to the cluster.

## 5. Prior art and what Fabric adds

The landscape splits into four groups.

*Unified compute layers.* SkyPilot runs a single task definition across clouds, Kubernetes, Slurm, and user-provided SSH machines, and has recently added agent-facing tooling. It is the strongest candidate to back the cloud adapter (and possibly the Slurm adapter). Its limits for our purposes: the environment model is imperative setup scripts plus cluster reuse rather than content-addressed environments, and its SSH node pools bootstrap a Kubernetes (k3s) layer that requires passwordless sudo — often unavailable on shared academic machines.

*Federated function services.* Globus Compute places lightweight endpoints on HPC resources and dispatches Python functions through a cloud service using only outbound connectivity, which neatly sidesteps login-node firewall friction. It is attractive as an optional adapter for facilities that support it, but it largely assumes the endpoint's pre-existing environment and introduces a third-party service dependency.

*Workflow managers.* Nextflow and Snakemake long ago proved the per-task environment binding model — a conda environment or container per step — with executors for local, Slurm, and cloud. Fabric adopts exactly those semantics, but as an interactive, agent-native service rather than a batch workflow file. Ray's `runtime_env` is the closest single precedent for attaching an environment spec to a task and materializing/caching it on the remote side, though it works only within a Ray cluster.

*Environment tooling.* pixi produces per-project lockfiles pinning the full transitive dependency closure across platforms, installs without root, ships as a single static binary, and pairs with `pixi-pack` for delivering pre-resolved environments to machines without package-index access. Apptainer covers container execution on clusters where Docker is disallowed. Module systems cover site-licensed software. The recent availability of full GPU (CUDA) stacks as conda-forge packages makes even accelerated environments declaratively specifiable.

What no existing tool provides is the composition: **declarative environment specs resolved to content-addressed realizations on arbitrary heterogeneous sites, combined with content-addressed data staging, exposed as a compact asynchronous tool API for an LLM agent.** That composition is Fabric. Roughly 80 % of the machinery is existing tools; the new work is the spec → hash → realization model, the site capability system, and the agent-facing surface — and that new work is cleanly separable as a standalone module.

## 6. Document map

Document 01 defines the architecture and core object model. Document 02 specifies the compute-site abstraction, capability probing, adapters, and the remote bootstrap protocol. Document 03 specifies the environment system: spec schema, composition, resolution, realization strategies, and caching. Document 04 specifies the data plane: content addressing, staging, transfer adapters, result manifests, and provenance. Document 05 specifies the agent interface: the tool API, the asynchronous job model, error taxonomy, and operational guardrails. Document 06 lays out the implementation roadmap, testing strategy, risks, and open questions.

## 7. Glossary

A **Site** is a registered place where tasks can run (local machine, SSH host, Slurm cluster, cloud pool), described by a capability record. An **EnvSpec** is a declarative description of a software environment; a **Lockfile** is its fully pinned resolution; the **EnvID** is the content hash of the lockfile and serves as the universal cache key. A **Realization** is a concrete, usable materialization of an EnvID on a specific site (a prefix directory, a container image, a module recipe). A **DataRef** is a content-hashed reference to a file or directory tree. A **Task** is one execution request (environment, inputs, command, resource needs); a **Job** is a Task in flight on a Site, with a handle, lifecycle states, and logs. The **Workspace** is the per-project directory on the user's machine holding inputs, outputs, manifests, and provenance records.

## 8. References

SkyPilot: https://docs.skypilot.co · https://github.com/skypilot-org/skypilot
Globus Compute: https://www.globus.org/compute
pixi and pixi-pack: https://pixi.sh · https://github.com/Quantco/pixi-pack
Apptainer: https://apptainer.org
PSI/J (portable batch-scheduler interface): https://exaworks.org/psij
Ray runtime environments: https://docs.ray.io/en/latest/ray-core/handling-dependencies.html
Snakemake: https://snakemake.readthedocs.io · Nextflow: https://nextflow.io
