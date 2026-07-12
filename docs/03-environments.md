# Fabric — Document 03 — Environments

This is the load-bearing document. The central claim: if environments are *declarative specs resolved to content-addressed lockfiles*, then remote heterogeneity reduces to choosing a per-site *realization strategy*, and reuse reduces to a hash lookup. Layering — the current app's install-on-top-of-base behavior — becomes spec composition plus caching, not a structural mechanism.

## 1. EnvSpec schema

```yaml
envspec:
  name: hep-analysis            # human label, not identity
  platforms: [linux-64, osx-arm64]   # solve targets; sites add theirs on demand
  channels: [conda-forge]
  deps:
    conda:
      - python =3.12
      - root >=6.32          # HEP framework — on conda-forge
      - uproot
      - awkward
      - numpy
      - scipy
      - matplotlib
      - iminuit
    pypi:
      - zfit ==0.24.*
  variants:                   # optional per-platform additions
    linux-64:
      conda: [cuda-version =12.4, cupy]
  modules: []                 # site-module requirements, e.g. ["espresso/7.2"]
  container_base: null        # optional OCI base for container realizations
  env_vars: {OMP_NUM_THREADS: "{{cpus}}"}
  post_install: []            # discouraged; escape hatch for odd pip installs
```

Design notes. `platforms` makes cross-platform intent explicit: the user's laptop may be `osx-arm64` while every remote is `linux-64`, and the lockfile pins both so the *same spec* is usable everywhere. `modules` declares dependencies on site-provided software that cannot come from package channels (licensed compilers, vendor MPI, site-tuned codes); a spec with modules is only satisfiable on sites whose inventory contains them, which the placement filter enforces. `post_install` exists because reality contains packages installable only via a bespoke command; it is discouraged because it weakens hashing guarantees (see §3). GPU stacks are ordinary conda dependencies now that CUDA libraries ship on conda-forge, pinned via `cuda-version` against the site's driver capability from the probe record.

## 2. Composition: layering as spec algebra

The app's existing UX — "base environment plus extras for this task" — is preserved, but at the spec level:

```yaml
envspec:
  extends: env-base-physics@sha256:41c0…   # parent spec, pinned by spec hash
  deps:
    conda: [emcee, corner]                  # merged into parent's deps
```

Merge rules are simple and total: child channels prepend; dependency lists concatenate with child constraints overriding parent constraints on the same package; scalar fields override. The merged spec is then resolved *as a whole* — one fresh solve, one new lockfile, one new EnvID. Fabric never installs into an existing realized environment to produce a derived one. This is the key correctness decision: incremental in-place installs are order-dependent and drift-prone (the resulting environment depends on history, not on the spec), which is precisely what makes today's local layering hard to reproduce remotely. Under whole-spec resolution, "layering" survives as UX and as a *cost* optimization, because the package caches below make a derived environment cheap to build (§6).

Two composition patterns cover the project lifecycle. A *project base* spec holds the tools the whole analysis shares; task specs extend it with one or two additions. When a task-level addition proves durable, the agent proposes promoting it into the base — a one-line spec edit producing a new base version, with the old EnvID still valid for reproducing past results.

## 3. Resolution and identity

Resolution takes a (merged) spec and produces a lockfile pinning every conda and PyPI package — version, build, and archive digest — for every platform in `platforms`. Implementation-wise this is pixi's solver (rattler + uv) driven as a library, with the lockfile stored in Fabric's local CAS. Solving requires index access, so it always happens on the user's machine (or another internet-connected point), never on an air-gapped compute node — one of the quiet architectural wins of separating resolution from realization.

Identity: `EnvID = sha256(canonicalized lockfile)`. Canonicalization sorts packages and strips solver metadata so identical resolutions hash identically. Consequences worth stating explicitly: two different specs that resolve to the same package set share realizations; the same spec re-solved after channel updates yields a *new* EnvID (old results remain reproducible against the old one); and a spec is *re-solved only on explicit request* ("update environment"), never implicitly, so task submission is deterministic.

Anything that escapes locking weakens this. `post_install` commands are therefore hashed by their text into the EnvID (best effort), and specs using them are marked `weakly-reproducible` in the UI. `modules` are hashed by name+version string; Fabric cannot pin a site module's actual content, and honestly represents that limit in provenance records rather than pretending otherwise.

## 4. Realization strategies

A realization makes an EnvID usable on a site. Four strategies, one selected per `(EnvID, site)` by capability:

**`prefix` — install from lockfile in place.** The shim runs the bundled static `pixi` to install the locked packages into `$FABRIC_ROOT/envs/<EnvID>`. Requires index/network access from wherever the install runs (login node suffices if storage is shared with compute nodes). Package archives land in a shared per-site package cache, and pixi hardlinks files from cache into environments — so two environments sharing 95 % of packages share ~95 % of their bytes and the second build takes seconds. This is the default on workstations and network-open clusters, and it is what makes spec-level layering cheap in practice.

**`packed` — build elsewhere, push, unpack.** For sites without usable network: Fabric builds the environment locally (or reuses the local realization) for the site's platform, packs it (pixi-pack-style archive of the locked packages plus an offline installer), pushes it over the control or data channel, and the shim unpacks and verifies into the same cache layout. Costs bandwidth on first use; identical reuse thereafter. Cross-platform note: packing for `linux-64` from an `osx-arm64` laptop is a *pack of downloaded archives*, not a native install, so no emulation is needed — the archives are platform-tagged artifacts from the lockfile.

**`container` — OCI/Apptainer image.** From the lockfile, Fabric generates a deterministic image (base + locked install) and delivers it as Docker/OCI (cloud) or `.sif` (clusters with Apptainer). Chosen when the site prefers containers, when `container_base` is set (system libraries beyond conda's reach), or when node-local scratch is too small for unpacked trees but images can sit on shared storage. Image tags are the EnvID, so registries and `.sif` caches deduplicate naturally.

**`modules+prefix` — hybrid.** The activation prelude performs the spec's `module load`s, then activates a (possibly minimal) prefix environment on top. Ordering and PATH precedence are fixed by convention (modules first, prefix overrides) and recorded in the realization so behavior is stable.

Selection is a small decision table over the capability record — roughly: compute-node internet and shared storage → `prefix`; no internet but Apptainer and image-friendly storage → `container`; no internet, no container runtime → `packed`; `modules` in spec → hybrid variant of whichever base strategy applies. The table is a pure function and unit-testable; the agent can also request a strategy explicitly when diagnosing site quirks.

## 5. Activation contract

Whatever the strategy, the job script interacts with exactly one interface: `fabric-shim activate <EnvID>` emits the shell prelude (module loads, PATH/env exports or `apptainer exec` wrapping) and the task command runs inside it. Tasks therefore never encode strategy-specific logic, and a task that ran via `prefix` on the workstation reruns via `container` on the cluster without modification. The contract includes a fixed working-directory layout (`inputs/` read-only, `outputs/` writable, `tmp/` node-local when available) and the guaranteed env vars (`FABRIC_JOB_ID`, `FABRIC_ARRAY_INDEX`, resource counts).

## 6. Caching, verification, and garbage collection

Three cache tiers, all content-addressed. The *package cache* (per site) holds package archives shared across environments — the unit of network savings. The *realization cache* (`envs/`, `imgs/`) holds usable environments keyed by EnvID — the unit of startup savings. The *lockfile CAS* (local) holds resolutions — the unit of reproducibility. Builds are atomic (temp dir → fsync → rename), and a realization records a post-build spot-check (interpreter runs, key imports succeed) so a corrupted unpack is caught at build time rather than mid-job.

GC is policy-driven and conservative: realizations carry last-used timestamps; candidates are those unused for N days *and* not referenced by any pinned project; the user (or agent, with confirmation) triggers sweeps, and per-site quota pressure from the probe record raises proactive suggestions. Nothing is ever deleted implicitly on shared systems.

## 7. The interactive path: mutable session envs

Agent-driven analysis has a genuinely interactive mode — "try importing X… now add Y" — where a full re-solve per step would be irritating even at seconds each. Fabric supports a *session environment*: a scratch clone of a realized environment on the site where the session runs, into which the agent may install incrementally. The crucial rule is that session envs are **unhashed, non-citable, and single-site**: no task result destined for the project record may run in one. When the exploration stabilizes, the agent *snapshots* the session — Fabric reads the actually-installed package set, synthesizes the minimal spec delta on top of the base, re-solves properly, and produces a real EnvID. The re-run of the final computation under the real EnvID is cheap (shared package cache) and is what enters provenance. This gives interactive speed without sacrificing the identity model — the design's answer to "layer on demand, but reproducibly."

## 8. Worked examples

*HEP fit stack* — the spec in §1: pure conda-forge, realizes as `prefix` everywhere, `packed` on the air-gapped cluster; ~3 GB realized, ~40 s first build on the workstation, instant thereafter.

*Astronomy imaging* — `astropy, photutils, reproject, dask`, plus `variants.linux-64: [cupy, cuda-version=12.x]` for the GPU cloud site; one spec, CPU on the laptop, GPU realization in the cloud.

*Plane-wave DFT on the cluster* — `modules: ["espresso/7.2"]` plus a small conda prefix (`ase, numpy, matplotlib`) for pre/post-processing; realizes only where the module exists; provenance records the module identity as site-attested rather than content-hashed.

*Lattice/Monte Carlo code built from source* — the project's own C++/CMake code is *task input data* (a DataRef of the source tree) compiled in the job's prologue inside a spec-provided toolchain (`cxx-compiler, cmake, ninja, fftw, hdf5`); build artifacts cache under `hash(source tree, EnvID)` per site. Keeping user code out of the environment and in the data plane keeps env churn near zero while code iterates daily — the compile cache does the heavy lifting.
