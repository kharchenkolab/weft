# Fabric — Document 01 — Architecture

## 1. Position in the host application

Fabric is a standalone module with its own state, embedded in (or run alongside) the desktop app. The LLM agent never touches sites, environments, or transfers directly; it calls Fabric's tool API (Document 05). The app's UI reads the same state to render job dashboards, transfer progress, and environment inventories. Nothing in Fabric depends on the app's domain logic, which is what makes it independently testable and potentially reusable.

```
┌────────────────────────────── desktop app ──────────────────────────────┐
│  UI (chat, notebooks, dashboards)         LLM agent (planning loop)     │
│         │  reads state                        │  tool calls             │
│         ▼                                     ▼                         │
│  ┌───────────────────────── Fabric core ─────────────────────────────┐  │
│  │  Tool API (MCP-style)                                             │  │
│  │  ├─ Task manager        (submit / status / logs / cancel)         │  │
│  │  ├─ Env manager         (spec → lockfile → EnvID → realization)   │  │
│  │  ├─ Data manager        (DataRefs, staging plans, caches)         │  │
│  │  ├─ Site registry       (capabilities, probing, health)           │  │
│  │  └─ State store         (SQLite) + event bus + audit log          │  │
│  └───────────┬───────────────┬───────────────┬────────────┬──────────┘  │
└──────────────┼───────────────┼───────────────┼────────────┼─────────────┘
               ▼               ▼               ▼            ▼
        local adapter    ssh adapter    slurm adapter   cloud adapter
        (subprocess)    (shim over      (login node +   (wraps SkyPilot)
                         SSH session)    sbatch/squeue)
```

## 2. Core object model

Five entities carry the whole design. Their identity rules matter more than their fields.

**Site.** A registered execution target. Identity is a user-chosen name (`beamlab`, `hpc-univ`, `cloud-gpu`). Carries connection config (SSH alias, scheduler account/partition defaults, cloud credentials reference), a *capability record* produced by probing (Document 02), and health status. Capabilities — OS/arch of compute nodes, scheduler type, container runtimes present, internet reachability from compute nodes, storage roots and quotas, GPU inventory, available module names — drive every downstream decision.

**EnvSpec / Lockfile / EnvID.** An EnvSpec is the declarative environment description (Document 03). Resolution produces a Lockfile pinning every package for every target platform. `EnvID = hash(canonical lockfile)`. Two specs that resolve identically share an EnvID and therefore share every cached realization. The EnvID, not the spec, is what tasks reference at execution time.

**Realization.** A record `(EnvID, Site) → {strategy, location, state}` where strategy is one of `prefix` (pixi-installed directory), `packed` (pushed pre-resolved archive), `container` (OCI/Apptainer image), or `modules+prefix` (site modules layered with a prefix). Realizations are immutable once `ready`; a failed realization is recorded with its log so the agent can adapt.

**DataRef.** `dref:<sha256>` plus size and type (file or tree; trees hash a canonical manifest of their contents, git-style). The data manager tracks *locations*: which sites hold which DataRefs in their caches. Staging is then a set-difference computation (Document 04).

**Task / Job.** A Task is the pure request:

```yaml
task:
  env: env:9f3a…            # EnvID (or inline spec; Fabric resolves first)
  inputs:
    - {ref: "dref:ab12…", mount_as: "data/run2189.h5"}
  code: {ref: "dref:cc90…", mount_as: "scan.py"}     # code is just data
  command: "python scan.py --grid grid.json --out results/"
  outputs: ["results/"]
  resources: {cpus: 32, mem_gb: 64, gpus: 0, walltime: "02:00:00"}
  site: "hpc-univ"           # or "auto" → placement (§5)
```

A Job is a Task bound to a Site with a scheduler-native handle and a lifecycle. Tasks are content-hashable too (`TaskID = hash(env, inputs, code, command)`), which gives memoization for free: resubmitting an identical task can return the prior result manifest unless the caller opts out.

## 3. Job lifecycle

```
PENDING → RESOLVING_ENV → STAGING → QUEUED → RUNNING → COLLECTING → DONE
                │             │        │         │          │
                └─────────────┴────────┴────┬────┴──────────┘
                                            ▼
                                   FAILED(stage, error) | CANCELLED
```

`RESOLVING_ENV` covers lockfile solve (if given a spec rather than an EnvID) and realization on the target site — usually a cache hit and instantaneous. `STAGING` transfers missing DataRefs. `QUEUED` exists only on scheduler-backed sites. `COLLECTING` hashes declared outputs into new DataRefs, writes the result manifest, and (per policy) pulls previews or full outputs back. Every state transition is an event on the bus with a timestamp and payload; the UI and the agent's notification stream are both consumers.

Failures carry a structured `(stage, code, detail, remediation_hints)` tuple rather than raw stderr alone (taxonomy in Document 05), because the agent's ability to *recover* — add a missing dependency and re-realize, choose another site, shrink the resource ask — depends on machine-readable causes.

## 4. Control flow of one submission

For scenario S2 (Slurm scan), end to end:

1. Agent calls `ensure_env(spec)` → Fabric returns an existing EnvID in milliseconds (spec unchanged since last week; lockfile cached).
2. Agent calls `submit(task)` with `site: "hpc-univ"`. Task manager validates resources against site capabilities (32 CPUs ≤ partition max, walltime within limits) and persists the Job as `PENDING`.
3. Env manager checks `(EnvID, hpc-univ)` → no realization. Capability record says: no internet on compute nodes, Apptainer present, generous scratch. Strategy selector picks `packed` (Document 03 §6); the archive is built locally from the lockfile, pushed over SSH, unpacked into the site's env cache, verified, recorded `ready`.
4. Data manager computes the staging plan: of the task's three DataRefs, two already exist in the site cache; one 2 GB file is missing → rsync via login node to scratch CAS, verify hash, register location.
5. Slurm adapter renders a batch script (activation prelude + command + output capture), submits via `sbatch` on the login node, records the Slurm job id, transitions to `QUEUED`.
6. A lightweight poller (or `sacct` batch query for all outstanding jobs on that site) advances state; log tails are fetched on demand rather than streamed continuously to keep login-node load negligible.
7. On completion, the epilogue hashes `results/`, writes `manifest.json` (Document 04 §5), and Fabric pulls the manifest plus small previews. The Job is `DONE`; the agent receives the manifest, not gigabytes.

## 5. Placement (`site: "auto"`)

Placement is deliberately simple in v1: filter sites whose capabilities satisfy the task (arch, GPUs, memory, module requirements, walltime limits), then rank by a weighted score of (a) environment already realized, (b) inputs already cached, (c) expected start latency (interactive sites beat queued sites for short tasks), (d) user-set preference/cost weights. Ties and low-confidence choices are surfaced to the agent as a ranked list rather than silently decided — the agent (or the user) confirms. Sophisticated cost models are explicitly deferred; the interface (a scoring plugin) leaves room.

## 6. State, concurrency, and crash recovery

All durable state lives in a single local SQLite database (sites, env specs/lockfiles/realizations, DataRef locations, jobs, events, audit log) plus an on-disk CAS for lockfiles and manifests. SQLite is sufficient because writers are one process and cardinalities are modest (thousands of jobs, not millions).

Two invariants make crash recovery tractable. First, *all remote effects are idempotent or fenced*: env realizations are built in a temp directory and atomically renamed into the cache keyed by EnvID (a re-run either finds the finished realization or redoes the build); transfers verify hashes before registering locations; submissions write the scheduler job id to the store *before* confirming, so a crash between `sbatch` and the DB write is reconciled at startup by matching a Fabric-generated job tag embedded in the job name. Second, *remote state is the source of truth for running jobs*: on restart, Fabric reconciles every non-terminal Job against the site (scheduler query or shim query) rather than trusting the local snapshot.

Concurrency control is per-resource: one realization build per `(EnvID, site)` (later requesters wait on the same future), one transfer per `(DataRef, site)`, unlimited parallel jobs. Site adapters cap concurrent SSH sessions per host (multiplexed over a ControlMaster connection) to stay polite on shared login nodes.

## 7. Process model

Fabric core runs as a local service (or in-process library with a service façade) exposing the tool API. Remote sites get at most a tiny *shim* — a static helper binary or POSIX script set installed under the Fabric root on first use — that implements: run-with-activation, tail-log, probe, hash-tree, and unpack-verify. Plain-SSH sites use the shim per-invocation (no daemon, nothing listening); scheduler sites additionally use the shim inside batch scripts for the prologue/epilogue. Cloud sites delegate lifecycle to SkyPilot and use the same shim inside the provisioned instance. Persistent daemons on remote machines are deliberately avoided in v1: they complicate operational hygiene on shared systems and are unnecessary for the target workloads.

## 8. Extension seams

Three plugin interfaces cover anticipated growth without core changes: `SiteAdapter` (Document 02 §6 lists the required methods), `Realizer` (Document 03 §6), and `TransferMethod` (Document 04 §4). A fourth, `Notifier`, lets the app route job events to UI toasts, the agent's context, or external channels. Everything else — placement scoring, preview generation, provenance export — hangs off the event bus.
