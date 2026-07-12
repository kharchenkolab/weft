# Fabric — Document 02 — Compute Sites

## 1. What a site is

A site is anything that can run a process on demand under the user's identity. Fabric models four kinds in v1 — `local`, `ssh`, `slurm` (reached through a login node), and `cloud` (SkyPilot-backed) — behind one adapter interface. A fifth, `globus-compute`, is sketched as a later optional adapter for facilities that host endpoints. The design premise is that *sites differ in capabilities, not in kind*: a workstation and a cluster login node are both "SSH-reachable POSIX machines"; what differs is whether execution goes through a scheduler, whether compute nodes can reach the internet, and what runtimes exist. Encoding those differences as data (the capability record) rather than as code keeps adapters small.

## 2. Site registration

The user registers a site through the app (the agent can propose it, the user confirms — credentials and remote systems are user-owned). Registration input is minimal:

```yaml
site:
  name: hpc-univ
  kind: slurm
  ssh: {host: login.hpc.univ.edu, alias: hpc}   # uses ~/.ssh/config; Fabric adds nothing
  scheduler: {account: phys-lab, default_partition: standard}
  storage:
    home: ~                    # discovered if omitted
    scratch: /scratch/$USER    # preferred large-file root
  policy:
    max_concurrent_jobs: 50
    fabric_root: $SCRATCH/.fabric   # where caches/realizations live
```

SSH connectivity reuses the user's existing configuration (aliases, ProxyJump, keys, agents). Fabric never stores private keys; it stores only references to the user's SSH setup and invokes the system `ssh`. This inherits multi-hop and MFA setups for free — with the caveat that interactive-MFA sites work best with SSH connection multiplexing (one authenticated master connection reused for the session), which the SSH adapter enables by default via `ControlMaster`/`ControlPersist`.

## 3. Capability probing

On registration (and on demand thereafter), Fabric runs a probe. The probe is a single POSIX script executed via the shim that emits a JSON capability record; on scheduler sites it optionally submits a tiny probe job to characterize *compute* nodes separately from the login node, because they routinely differ (different arch, no internet, different mounted filesystems).

```json
{
  "probed_at": "2026-07-11T14:02:11Z",
  "login":   {"os": "linux", "arch": "x86_64", "glibc": "2.34"},
  "compute": {"os": "linux", "arch": "x86_64", "internet": false,
               "gpus": [{"model": "A100-40GB", "count_max_per_node": 4}]},
  "scheduler": {"type": "slurm", "version": "23.11",
                 "partitions": [{"name": "standard", "max_walltime": "48:00:00",
                                  "cpus_per_node": 64, "mem_gb_per_node": 256},
                                 {"name": "gpu", "gres": "gpu:a100:4"}]},
  "runtimes": {"docker": false, "podman": false, "apptainer": "1.3.1"},
  "modules":  {"system": "lmod", "count": 412},
  "storage":  {"home":   {"path": "/home/u123",     "quota_gb": 20,  "free_gb": 4},
                "scratch": {"path": "/scratch/u123", "quota_gb": 5000, "free_gb": 3200},
                "shared_between_login_and_compute": true},
  "net":      {"login_internet": true, "outbound_ports_open": true}
}
```

Three probe results shape everything downstream. `compute.internet` decides whether environments can be solved/installed in place or must be delivered pre-built (Document 03). `runtimes.apptainer` enables the container realization path. `storage` decides where the Fabric root lives — never in a quota-tight home directory — and whether login-node staging is visible to compute nodes (if not, staging must route through the scheduler's copy mechanism or a shared path, an edge case flagged at registration rather than discovered at job time).

Probes also record the *module inventory* lazily: rather than caching 412 module names, Fabric queries `module avail <name>` on demand when a spec references one, and caches positive/negative answers per site.

## 4. Remote bootstrap protocol

First contact with any POSIX site follows one sequence, requiring nothing but a working `ssh` and a writable directory:

1. `ssh site 'echo $HOME; uname -sm; df …'` — pre-probe, choose `fabric_root`.
2. Push the shim: a small set of POSIX scripts plus two static binaries — `pixi` (environment realization) and a tiny `fabric-shim` helper (hashing, atomic unpack, JSON probes). Total under ~50 MB, placed under `fabric_root/bin`. Static binaries mean no interpreter or library assumptions about the host.
3. Run the full probe; store the capability record.
4. Write a site marker file with the Fabric version, enabling cheap "is bootstrap current?" checks and clean upgrades (new version → push new binaries alongside, atomic symlink flip).

Everything Fabric places on a remote site lives under the single `fabric_root` (default `$SCRATCH/.fabric` or `~/.fabric`), with a documented layout, so a user can inspect or delete Fabric's entire footprint with one command:

```
$FABRIC_ROOT/
  bin/            # shim + pixi (static)
  envs/<EnvID>/   # realizations (prefix or unpacked archives)
  imgs/<EnvID>.sif
  cas/<sha256[:2]>/<sha256>   # data cache (Document 04)
  jobs/<JobID>/   # per-job sandbox: mounted inputs (links), outputs, logs, manifest
  tmp/
```

For `cloud` sites the bootstrap folds into instance provisioning (SkyPilot file mounts + setup), and `fabric_root` sits on the instance disk or an attached volume, optionally backed by an object-store cache so that re-provisioned instances warm up quickly.

## 5. Execution semantics per adapter

**local.** Direct subprocess under the app. The job sandbox lives in the workspace; "staging" is hardlinking from the local CAS. This adapter is also the reference implementation the others are tested against — every adapter must produce byte-identical job sandboxes and manifests for the same task.

**ssh.** One-shot execution: `ssh site fabric-shim run --job <id>` starts the command inside the activated environment with stdout/stderr teed to `jobs/<id>/log`, detached via `setsid`/`nohup` with the PID and exit-code file recorded. Fabric monitors by polling the shim (`status`, `tail`) over the multiplexed connection. Detachment matters: jobs survive dropped SSH connections and laptop sleep; on reconnect Fabric reconciles from the PID/exit-code files. No long-running remote daemon.

**slurm.** The adapter renders a batch script from a template: scheduler directives from `resources` (translated per partition), then `fabric-shim prologue` (verify realization + inputs present), activation, the command, and `fabric-shim epilogue` (hash outputs, write manifest, record exit). Submission, status, and cancellation go through `sbatch`/`squeue`/`sacct`/`scancel` on the login node, batched: one `squeue`+`sacct` poll per site per interval covers all outstanding jobs. Array jobs map to a first-class Task field (`array: {count: N, var: FABRIC_ARRAY_INDEX}`) because parameter scans are the dominant cluster workload in the target domain; the epilogue then produces one manifest per element plus a roll-up. The adapter interface is written against a minimal scheduler-verb abstraction so a later PBS/LSF port (possibly via PSI/J) touches templates, not logic.

**cloud (SkyPilot-backed).** The adapter translates a Task into a SkyPilot task: resource ask → `resources`, staging → file mounts from the local CAS (or an object-store bucket cache), realization → container image or setup that installs from the lockfile, and reuses SkyPilot's cluster lifecycle (launch, reuse, autostop). Fabric treats the provisioned cluster as an ephemeral site whose capability record is known from the resource ask. Cost guardrails (Document 05 §6) gate launches.

**globus-compute (later, optional).** Where a facility runs endpoints, the adapter submits function-shaped tasks through the service, gaining outbound-only connectivity (no SSH to login nodes) at the cost of a service dependency. It slots in cleanly because the adapter interface is already asynchronous and handle-based.

## 6. The adapter interface

```python
class SiteAdapter(Protocol):
    def probe(self) -> CapabilityRecord: ...
    def ensure_bootstrap(self) -> None: ...
    def put(self, local: Path, remote: str) -> None      # small control files
    def get(self, remote: str, local: Path) -> None
    def run_shim(self, argv: list[str]) -> ShimResult     # probe/status/tail/hash/unpack
    def submit(self, job: RenderedJob) -> JobHandle: ...
    def poll(self, handles: list[JobHandle]) -> list[JobState]: ...
    def cancel(self, handle: JobHandle) -> None: ...
    def logs(self, handle: JobHandle, tail: int) -> str: ...
```

Bulk data movement is deliberately *not* here — it belongs to the data plane's `TransferMethod`s (Document 04), which may use entirely different channels (rsync, rclone, Globus) than the control channel. Keeping control and data planes separate is what lets a Slurm site use SSH for control and Globus for terabyte staging simultaneously.

## 7. Health, drift, and failure handling

Sites are living systems: quotas fill, modules get renamed, partitions change limits, maintenance windows happen. Fabric treats the capability record as a cache with staleness rules — storage numbers refresh opportunistically on every shim call, the full probe re-runs weekly or on demand — and treats *capability-violation errors* (e.g., submission rejected for exceeding new partition limits) as triggers for an immediate re-probe plus a structured error to the agent, which can then re-plan. Connectivity failures mark a site `unreachable` with exponential back-off; queued local intents (submissions, transfers) persist and resume, which is the behavior a laptop user expects when moving between networks.

A small but important rule: Fabric never retries *the user's computation* on its own. Infrastructure operations (transfers, polls, submissions) retry idempotently; failed science jobs are reported with causes, and the retry decision belongs to the agent and user, who may want to change the task instead.
