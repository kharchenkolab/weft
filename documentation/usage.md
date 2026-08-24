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
| `inputs` | `[{ref, mount_as}]` — sandbox-relative mounts, READ-ONLY and ENFORCED (0444; 0555 exec): pipelines do not modify their inputs. Staging is zero-copy where the filesystem allows — a read-only blob hardlinks, a blob sharing its inode with your registered original is CoW-cloned (its own inode, your file untouched), byte copy only as the last resort. A tool that must mutate its input should copy inside the sandbox first (`cp in.dat work.dat`). The chmod loophole is deliberate: permissions guard accidents, and the verify fence detects falsification at next use |
| `code` | same shape; code is just data (hash-addressed like everything) |
| `outputs` | declared result paths — LITERAL, no glob patterns: plain files (`plot.svg`) or directories (`results/`, trailing slash); a declared output that was not produced fails the job; the same path may not be declared twice or coincide with an input's mount |
| `resources` | `cpus, mem_gb, gpus, walltime, partition` — validated against site capabilities AND user policy |
| `site` | site name or `"auto"` |
| `array` | N: fan out N element jobs with `WEFT_ARRAY_INDEX` = 0…N-1 |
| `env_vars` | exported in the job; `{{cpus}}`/`{{mem_gb}}`/`{{gpus}}` templated; `WEFT_*` is weft's reserved facts namespace (refused — the exports would clobber `WEFT_CPUS`/`WEFT_JOB_ID`/`WEFT_STORAGE_*`) |
| `after` | job_ids that must be DONE first — pipelines without polling; a failed upstream fails this job as `task.dep_failed` (it never starts) |

Weft-launched interpreters are HERMETIC: `PYTHONNOUSERSITE=1` is set
in every launcher (tasks — including `env: null` bare-node runs —
kernels, and session exec), so `~/.local` site-packages never leak in
(a version-matched broken user-site build otherwise shadows even
managed envs). Opt out per task with `env_vars:
{"PYTHONNOUSERSITE": ""}`.

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

Extends solves survive CHANNEL ROTATION: the delta solve resolves the
parent's packages from the parent's OWN recorded lock (synthesized into
a solve-local channel), so a base whose exact builds have aged off
bioconda/conda-forge still extends — published packs have no shelf
life. The child's lock still points at the packages' real homes, and
the child inherits the parent spec's channels (child's prepend). A spec
naming bioconductor-* packages without the bioconda channel gets a
`channel_hint` warning on the ensure result (those packages live only
there).

`env_realize(env_id, site)` idempotently realizes an env on a site
— ready-and-intact is a fast no-op; a missing/demoted/evicted
realization rebuilds from the stored lock through the standard path.
This is the honest primitive for "make it usable there NOW": never
run a placebo task for the side effect (a fixed probe task collides
with memoization — the recorded manifest comes back and nothing
rebuilds; field finding). `env_repair` stays the force lever for a
realization the marker still wrongly claims.

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
| `env.post_link_scripts` | staged conda post-link scripts pixi won't run → `post_install` (pinned content, then rm the script) or site policy `post_link: "warn"` |
| `env.activation_failed` | activation didn't take; your command never ran → check activate.sh output in the job log (a clobbering site_prelude/module init is the usual cause) |
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

On a LIVE run the default retain records a PIN (captured when the run
settles — the caller usually means the eventual complete file). When
the files are already final but the run keeps living (a days-long
kernel whose root files no block can claim), `settle="now"` captures
the CURRENT bytes immediately: per-file sha256 lands in the sidecar
(the drift ledger), and the source is re-statted after placement — a
file that moved during capture is flagged `changed_during_capture`
with a `retain.unstable` event, never silently. Nothing is pinned on
this lane; literal includes that match nothing come back as `not_yet`,
and repeating the call is a new capture, not an error. A default-lane
pin over a settled snapshot refuses (`retain.keep_exists`) because its
settlement would overwrite the banked bytes — `run_forget` first, or
snapshot again.

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
DIRECTORIES are first-class rels (the .zarr-class store a run leaves
at its root): stat answers `{kind: "dir", mtime}` (no bytes — a
tree's size is a walk), `data_register(run=, rel=)` mints the same
tree ref the absolute-path door would, and byte reads refuse typed,
naming the levers (register + data_members, or read a member path).
**The observation principle**: *identity gates movement; observation
follows the bytes; every address vocabulary gets both tiers.* Moving
bytes (fetch, staging, task inputs) always goes through content refs
— hash-verified on arrival, so an input's name IS its content. But
OBSERVING bytes (list/stat/read/range) works wherever they sit, under
whichever vocabulary names them: run keys (`run_inventory`,
`run_file_stat/read/read_range`), raw site paths (`data_fingerprint`),
and content refs (`data_stat`, `data_read_range`). Note on previews:
refs have no preview verb by design — declared outputs already carry
previews in the result manifest at collection. (In the run verbs,
`target` is the run id: a job id or kernel id.)

`data_stat(ref | refs=[...], site=)` is live observation versus the
record: where the bytes actually sit right now — workspace CAS, each
registered location (reference-in-place home or site CAS) — with
`divergent` flagging record/reality mismatch. NON-MUTATING: it
testifies, it never demotes (staging's verify fence acts on
divergence; doctor/reconcile mutate). Trees are observed by SAMPLE
(first `sample` members, flagged `sampled`); the exact tree audit is
`data_fingerprint`.

`data_read_range(ref, rel=, offset=, length=, site=)` is the
ref-addressed range read — the SAME engine as `run_file_read_range`
(conformance-pinned), so identical semantics: past-EOF is empty +
`eof` + `size`, cap clamps with `capped`, a vanish is `data.missing`
retryable. Tree refs take `rel=` (a member path — the chunked-store
shape); file refs take none. Resolution prefers the workspace copy
(local pread), then registered locations; `site=` narrows the remote
candidates. A range slice is unverifiable by construction in any
scheme — a viewing tier; computation inputs go through whole-content
verified fetch.

Both range verbs BATCH whole members: `rels=[...]` (on
`data_read_range` for tree members, on `run_file_read_range` for run
files) answers N files in ONE remote invocation — the WAN cost is the
per-call round-trip floor (~2.4x RTT measured), so a chunk cascade
must never pay it N times. The call budget (the same 16 MiB cap)
defers the remainder EXPLICITLY: entries past it return in
`not_read`, the caller loops; absent members are per-entry typed
errors and never fail the batch. Singular reads are ONE round trip
too (the shim answers absence-or-size-plus-payload atomically — there
is no separate stat, and no stat-then-read race). ssh sites keep
their control connection warm for 600s between calls
(`control_persist` site config adjusts).

`data_stat(refs=[...])` batches its probes too: one shim invocation
per site regardless of how many refs' copies live there — safe to call
on a whole listing before rendering it.

### Evicting cached copies (footprint control)

`data_evict(ref, at=..., dry_run=False, force=False)` deletes ONE
recorded copy of a ref — a site's CAS copy (`at="local"`, `at="hpc"`)
or the workspace blob (`at="@workspace"`) — and never touches the
record: identity, provenance, and every other copy survive, so the
next task that needs the bytes re-stages them from wherever they
remain. The receipt says what happened: `bytes_freed`, `remaining`
(where copies still live), and for trees `evicted_members`/`kept`
(per-member last-copy partition — safe members go, sole-copy members
stay, both are named).

Refusals are typed, and `force=True` covers exactly the weft-owned
cases:

| refusal | meaning | force? |
|---|---|---|
| `data.last_copy` | no other live copy anywhere (keep anchors count) | yes — destroys the data, loudly |
| `data.pinned` | workspace copy is provenance-reachable (a run's input/output) | yes — the record survives, bytes go |
| `data.external_home` | a reference-in-place home: the USER's original files | **no — never** |
| `data.missing` | no copy recorded at that site | — |

`dry_run=True` runs the SAME evaluator and returns the same receipt
shape with `would_free_bytes` (a refusal embeds instead of raising —
render it in the confirm sheet). It is advisory: copy accounting is
record-based, like gc; the verify fence turns a lying record into a
loud re-transfer, and a live check is one `data_stat` away.
Locations rows on `data_describe` carry a typed `external: true` flag
for reference-in-place homes — no path-prefix parsing.

Relatedly, `run_forget` on a keep whose bytes were some ref's ONLY
copy reports those refs in the receipt (`record_only: [...]`):
identity and provenance survive; the bytes do not.

`run_file_read_range(target, rel, offset=, length=)` is the
TRANSPORT tier (vs `run_file_read`'s preview): a byte range served
without moving the whole file — pread on local sites, the shim's
byte-range lane elsewhere (containment re-asserted remote-side). It
returns `{bytes_b64, nbytes, size, eof, capped, at}`: offset past EOF
is NOT an error (empty + `eof` + `size` — answer 416 upstream); a
`length` over the per-call cap (16 MiB; `WEFT_RANGE_READ_CAP`, read
per call) clamps with `capped=true` — loop for more. A file that
vanishes mid-read is `data.missing` retryable, never a 0-byte
success. This is the backhaul for HTTP-Range serving of remote
chunked stores.

Panels and pollers must BATCH: `run_file_stat(target, rels=[...])`
answers N files with one target resolution, one keep lookup and ONE
stat invocation (per-file answers under `files`; a `../` entry
refuses the whole call); `run_inventory(targets=[...])` returns
recorded receipts per target with per-entry typed errors (an absent
receipt never fails the batch). The per-file forms cost two store
queries and a subprocess EACH — a 50-file poll loop through them is
the measured NFS-stall shape.

**Store locality**: `Weft(ws, state_dir=...)` (or `WEFT_STATE_DIR`)
relocates `state.db` to fast local disk when the workspace lives on
NFS (field-measured 28x per-query penalty); the CAS and all
content-addressed data stay in the workspace. state.db is the SYSTEM
OF RECORD — on purgeable scratch a purge orphans every run, env and
session the workspace knows about. One state_dir per workspace (a
marker refuses sharing — merged state would be silent corruption).
Reads take per-thread WAL connections and never wait on writers;
cross-process writers wait out contention (busy_timeout) instead of
erroring.
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
# the ~50 MB tools push runs in the BACKGROUND by default: register_site
# returns after shim+probe (seconds); the site row's `tools` state goes
# preparing → ready|partial|failed (sites_list/describe show it, a
# site.tools event fires on completion). Every mode is safe — the first
# env build on the site ensures/joins/heals the tools itself, and
# refuses with levers (pixi_source, WEFT_PIXI_VERSION, manual placement)
# if they truly cannot be provisioned. tools="sync" blocks until pushed
# (ready-on-return); tools="skip" defers entirely to first use.
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
# storage roles accept a LIST when a site has several long-term stores:
#   "large": ["/groups/phys/me", "/archive/phys"]
# the FIRST entry stays the one WEFT_STORAGE_LARGE path inside sandboxes;
# the full list rides sites_describe(site)["storage"]["roles"]. Malformed
# roles (empty list, non-string entries) are refused at registration.
# sites_describe(site)["compute"] is the digested hardware summary
# (gpus, cuda_driver, cores, mem, os/arch — the same compute_view that
# validates GPU asks), so consumers routing work by site capability
# don't parse raw capabilities.

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
w.session_freezable(s["session_id"])             # "can this still freeze?"
                                                 # WITHOUT minting/realizing:
                                                 # dry-run solve of the
                                                 # would-be snapshot; a real
                                                 # solve, a statement about
                                                 # NOW. Installer-tainted
                                                 # sessions: freezable + a
                                                 # grade_note (escape-hatch).
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

The materialization lane is chosen by **write-need, not base
temperature**: a pypi-only add never needs a writable prefix, so it
lays a **pylib overlay** on ANY base — built-here or adopted — with no
per-session clone at all (the ~10s CoW clone of a large prefix is
reserved for installs that actually mutate the prefix). The overlay:
the delta is resolved *with the base visible* (`pip --dry-run
--report`), only the missing closure is fetched (`--no-deps
--target`), and the layer composes over the base via `PYTHONPATH`
(persisted in the session's `overlay.sh`; `runtime` carries `pylib`
and the composed activation). conda adds DO need the prefix: on a
built-here base they clone it (CoW where the volume supports it); on
an **adopted/imported base** — a read-only pack or unpacked archive
whose packages the site's cache has never held (a fact fixed at
adoption and never re-probed) — the clone would re-download the entire
base from the index (1.6 GB in the field case; impossible on an
egress-restricted node), so they refuse with `session.cold_base` and
three levers: `extends_env` (mint a real delta env — the citable twin
of the same composition), run it where the base was *built* (warm
cache), or `full_clone=true` (fetch the whole base; needs egress).

A conda add — or a bespoke `session_run_installer` without a
declared `writes_to` layer — ARRIVING AFTER a pylib overlay refuses
(`task.invalid`) with two levers rather than silently switching
mechanisms:
`full_clone=true` upgrades the session in place — clone the base,
replay the overlay's recorded pypi delta into the clone, strip the
overlay — ordered crash-safe (the mode flips to `clone` LAST; if the
replay fails the typed error says so and the session STILL WORKS
through its overlay; retry converges). Or `snapshot` + start fresh
from the minted env. The result carries `upgrade_note` when an
upgrade absorbed an overlay. Pass `full_clone=true` on the FIRST
install to skip the overlay and clone up front (e.g. when you know
conda-level adds are coming).

Two honesty signals on the clone path: if the sessions directory and
the base sit on **different devices** (st_dev), CoW cannot apply and
the clone degrades to a full copy — the result carries
`cross_device_note` and a `session.cross_device` event fires (measured
~98s vs ~10s for a large prefix; fix: put sessions and the store on
one volume). And `sites_describe(site)["capabilities"]["storage"]
["reflink"]` reports whether the site root's volume supports CoW
clones (`true|false|"unknown"` — probed at registration with a real
`cp --reflink`/`cp -c` self-test), so consumers can gate eager
pre-warming on it.

**`ensure_available(target, request, verify=True)`** — the one-verb
install path: make an eco-tagged delta available in a session, prove
it, and report a single typed envelope `{satisfied, changed, attempts,
verified, runtime}` (shape pinned in
documentation/ensure_envelope.schema.json). Satisfaction is CHECKED
first — already-proven entries short-circuit (`changed: false`,
`attempts: []`) and are late-recorded; per-lane failures ride
`hints.attempts` verbatim; a site outage HALTS remaining lanes and the
verdict is the outage, never unavailability. One ensure per session at
a time (`state.conflict`, retryable, heartbeat-stale claims are taken
over).

Ranked mode: `ensure_available(target, ["RNetCDF"], lanes=["conda",
"cran"])` — YOUR ranking (no default), per-package independent chains,
a lane succeeds only if its postcondition passes (verify-in-loop; a
failed lane's record is retracted and the chain continues), outages
HALT, exhaustion is `env.unavailable_in_lanes` with every attempt.
The substrate speaks each lane's DIALECT (an R-namespace bare name is
`r-<lowercase>` on conda — one documented derivation, used by probe
too; attempts record the `spelling` used); dialect requires an
effective postcondition, and a bare name across cran+pypi is refused
as ambiguous (per-lane spellings `{"name": "X", "pypi": "x"}` are the
escape). `fast=False` pulls the snapshot's conflict check FORWARD:
pypi adds solve the full manifest at add time — a base-contradicting
leaf fails there as a typed `env.solve_conflict` (solver message,
`at: add-time`), and nothing is installed or recorded; the overlay
lane's shadow warnings (`shadows_base`) also ride the envelope's
attempts. Use it for capability installs where correctness beats
latency; the default stays the fast lane with the snapshot as the
deferred check. Failing solves persist their stderr as
`solve/<hash>/solve.err` and emit an `env.solve_conflict`/
`env.solve_failed` event — a swallowed exception no longer destroys
the forensics. `cran_repos=[urls]` names extra repositories for the cran
lane in EITHER mode (validated at intake; the attempt records the
`repositories` actually used, like `spelling`). `probe=True` returns
per-lane availability FACTS (404 is false; transport trouble is
"unknown", never false) with no mutation — and with `cran_repos` the
cran probe answers "unknown" outright: crandb indexes CRAN only, and
a package living in a secondary registry must never probe false.
`target={"env": env_id}` runs the ONE-solve extends path and returns
the same envelope (`lane: "extends_env"`, `outcome: "solved"`;
`cran_repos` becomes the spec's `r_repositories`). Enforcement is
at-realize by default — the note says so, `verified` stays `{}`, and
nothing realizes on your clock. Pass `site=` to add **verify-now**:
when the RESULT env already holds a ready realization on that site
(the idempotent re-extend shape), the claim is proven against it
live — `verified` populates and `verified_site` names the site; a
failing live check is a degraded-realization finding
(`env.realize_failed` with the `env_repair` lever), and an oracle
that cannot run stays "unknown" with enforcement still at realize.

Build failures below any lane carry the `missing_system_lib`
subclass when the log shows the classic configure/header/linker
shapes: `hints.failure_class`, `hints.missing_system`
(`{header|library|pkg_config: name}` when captured), and a remedy
naming the real lever — an isolated env with a full solve, never a
session-lane retry.

`session_install(..., verify=...)` runs a POSTCONDITION in the
composed runtime after the install — `verify=True` proves presence (and
any `==`/`>=` pins) with per-ecosystem defaults; an explicit dict
(`{"import": [...], "loads": [...], "versions": {name: "==X|>=X"}}`)
states exactly what must hold. Failure is a typed
`env.realize_failed` (`hints.postcondition: true`, got/want) and the
entries are NOT recorded — the snapshot only carries what was proven.
An oracle that could not run reports "unknown" (never failed/passed);
those entries stay unrecorded and a re-install converges.

**R is first-class**: `session_install(cran=[...])` composes a session
`rlib` over the base via `R_LIBS` on ANY base, frozen or built-here.
It speaks the spec's whole vocabulary — plain names, `"name ==X.Y.Z"`,
and `"owner/repo@ref"` github sources (routed via a self-bootstrapped
`remotes`; the snapshot's solve SHA-pins the ref); `cran_repos=[url]`
names extra CRAN-like repositories, recorded and emitted as the spec's
`r_repositories`. For installers no vocabulary covers,
`session_run_installer(cmd, writes_to="rlib"|"pylib")` declares the
write target as the session layer and runs over the read-only base
(recorded as a post_install step — which realizes FULL, not overlay:
prefer `session_install` when the addition fits) —
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
activation (`ns_wrap` ⇒ inside `unshare -rm`). For out-of-band helper
processes, `exec_template` is the ready-made form: execute
`shlex.split(template) + argv` ON the session's site and argv runs
inside the activated env, every mode, quoting and namespace handled.
Runtime queries are observation, not activity: they don't touch
`last_used`.

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
`job.state` with DONE/FAILED. Cancels are confirm-then-settle: after
`task_cancel` the job stays live until the scheduler agrees it is
gone; each unconfirmed poll resends and emits `job.cancel_retry`.
Lease deaths are `kernel.died` /
`service.exited`, each carrying `cause`
("walltime_exceeded"/"oom"/"cancelled"/"exited"/"lost") and, on
scheduler sites, the raw `slurm_state`. Duration-bearing events carry
explicit `seconds` (`session.materialized`, upgrade included) or
per-phase timings (`session.installed`: `seconds`, or
`resolve_s`/`fetch_s`/`total_s` on the pylib lane) — consumers
attribute latency from the payload, never by subtracting adjacent
event timestamps. Unknown kinds should be
ignored (new kinds are always additive).

Kernel blocks display like notebooks: a bare FINAL python
expression echoes its repr (`_` is set, None silent); R auto-prints
visible top-level values exactly like the console (`invisible()` and
assignments stay silent); julia shows the last non-nothing value.
Explicit print/cat is never needed just to see a value.

Kernel blocks may freely `os.chdir`/`setwd` — the working directory
persists across blocks (session state) while the driver's protocol
files and `$WEFT_BLOCK_DIR` are anchored to the sandbox (absolute),
so an ordinary chdir can neither kill the kernel nor scatter saved
artifacts. Likewise a `site_prelude`/activation that chdirs cannot
orphan a job's exit record (runner re-anchors).

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

w.jobs_where(state="FAILED", limit=50)      # enumerate: jobs — pass the
                                            # returned next_cursor back as
                                            # cursor= (keyset: concurrent
                                            # inserts never shift a page;
                                            # offset= kept but unreliable
                                            # past one page under writes)
w.list_envs(); w.list_kernels(); w.list_services()   # … and everything else
w.data_list(kind="tree", at="hpc",          # every DataRef: {ref, kind,
            limit=100, cursor=None)         # bytes, meta, locations
                                            # (typed external flag)};
                                            # keyset next_cursor
w.data_members(ref, limit=500)              # tree members in MANIFEST
                                            # order ({path, bytes, sha256};
                                            # links flagged) — the hashing
                                            # order, so streamed stores
                                            # prefetch by it; cursor =
                                            # member index
w.audit_tail(50)                            # one trail, user + agent
w.audit_tail(50, actor="agent:c-9",         # filters: actor / action /
             action="site.note",            # since (unix ts, inclusive);
             since=t0)                      # page back in history with
                                            # before_seq=<next_before_seq>
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
                      "lab/pkg@fix-branch",             # github → pinned SHA
                      "lab/mono/rpkg@v2"]},             # package in a subdir
    "system_requirements": {"cran_snapshot": "2026-07-01"},  # frozen forever
})
# omitted cran_snapshot defaults to UTC-today − 2 (concrete date, recorded
# in the layer — never the controller's local calendar)
env["layers"]                        # per-layer package counts, source builds
w.env_ensure(spec, dry_run=True)     # test a fix; nothing stored
w.env_why(env_id, "data.table")      # what pulls it in / the locked record
```

**The cran layer deltas against the conda layer** (measured on aba 1.2:
25 of 26 cran installs were re-building, from source, packages the
conda layer had binary-installed a minute earlier — 11.7 min → seconds).
Closure members the conda lock already provides on every platform
(`r-<name>`) are dropped from the layer at solve time and recorded in
`layers.cran.satisfied_by_conda` with both versions; top-level cran and
github asks always stay (an explicit ask is an explicit ask). At realize
the installer additionally skips anything visible on `.libPaths`, so
old locks realize fast too. Snapshot URLs in locks are PLATFORM-NEUTRAL;
the Posit binary segment (`__linux__/<codename>`) is applied per site at
realize from a live os-release read (unsupported distro or macOS →
plain source URL, honest and slower). Source builds run with
`Ncpus`/`MAKEFLAGS` parallelism, capped min(nproc, 8) — site policy
`max_build_cores` is the lever (login nodes are shared). Realization
narrates: `realize.prefix`(+`.done`) around the conda build,
`realize.layer`(+`.done`) per layer, `realize.progress`
{layer, done, total} every ~5 s inside a long install — a
multi-minute silent call reads as a hang and invites cancels
(observed live, twice).

Missing interpreter → `env.layer_conflict` names exactly what to add
(`=`/`==` version pins on the interpreter are fine — the check parses
constraints with the one shared grammar).
A spec may carry a `verify` block (same grammar as session verify=):
it is IDENTITY-NEUTRAL (never forks the EnvID) and is proven every
time the env realizes — build-time always (ready MEANS verified;
failure is `env.realize_failed` with `postcondition: true`),
adopt-time by default with a site-policy opt-out
(`policy: {verify_on_adopt: false}`). Verify blocks compose along
`extends_env` (base ∪ child; the child's version assertion wins).
A package name listed twice in one lane is refused at intake
(`task.invalid` naming both entries) — generators that splice a base
pin with caller packages must deduplicate.
Unknown deps key → the registered-solver list. Adding an ecosystem =
one Solver class + one registry entry (`solvers.default_solvers`).

### Kernels (incremental interactive execution)

A solved env auto-realizes at `kernel_start` (weft's errand, like
`session_start` and `task_submit` — `realize.*` events narrate a slow
first realize). `kernel_promote(kernel_id, blocks, label=)` labels the
minted job row (defaults to the kernel's label; display only, never
identity) and records a terminal `run_inventory` receipt synthesized
from the promoted artifacts — "terminal state implies a receipt"
holds for promoted jobs too; the bytes stay reachable through the
manifest's refs and, while the kernel sandbox lives, the KERNEL
target's `run_file_*`.

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
w.reconcile()                               # supervision AND re-drives of
                                            # driverless rows (see below —
                                            # supervision alone is automatic)
```

### Restarts and outages (embedder truth)

Weft's rows are the record consumers render, so a dead controller or a
dropped ssh window must never fabricate state. Two mechanisms:

**Resume at construction.** `Weft(resume=...)` decides what happens to
nonterminal jobs a previous controller left behind:

- `"poll"` (default): supervision re-attaches at construction — jobs
  with a scheduler handle go back to the site pollers (the first tick
  classifies from remote truth: still running resumes, exited collects,
  gone earns the two-strike node failure), deferred submits re-park
  their probe, and driverless rows are stamped
  (`queue_reason`, `job.driver_lost` event) but NOT re-driven. Zero
  transport happens on the constructing thread; an unreachable site
  lands in the poller's outage machinery, not in your constructor.
- `"full"`: additionally re-drives driverless rows (sandbox wipe +
  staging — the right setting for a long-lived embedding controller
  that owns its workspace). Re-drives are capped (3): a task whose
  staging repeatedly kills its controller fails honestly with
  `job.redrive_exhausted` instead of crash-looping forever.
- `"off"`: nothing happens until `reconcile()` — inspection tooling.

Before this, a controller kill mid-job left the row RUNNING forever
while the finished task's exit record sat on disk (aba bug3, reproduced
live). Multiple controllers over one workspace are safe: collection is
claimed in the store (one collector wins; a crashed collector's claim
goes stale), like the drive claim.

**Transport outages never mint job verdicts.** A `site.unreachable`
anywhere in the job lifecycle is site-scoped: pollers emit ONE
`site.unreachable` event and back off, running jobs wait it out
(remote state is the truth), and collection retries then parks
(`collect.deferred`). A submit cut by an outage PARKS instead of
failing (`job.deferred` event, `queue_reason` says why, the row's
`deferral` carries `{since, stage, delivered, attempts}`): when the
site answers again, jobdir truth decides — the run finished during the
outage → collected as DONE (`job.recovered`, found=exited); it is
still running → the live pid is adopted and supervision resumes
(found=running); nothing ever started → ONE re-drive
(`job.redriven`), then the honest failure. Scheduler sites get a
positive queue check by job name first (an sbatch whose reply was lost
may sit PENDING over an empty jobdir — a blind re-drive would run the
task twice). Parked limbo is bounded: past the site policy
`outage_requeue_grace_s` (default 3600 s) the job fails honestly with
`deferred_for_s` and the lever named in hints.

The trail's actor is set by the EMBEDDER at construction
(`Weft(default_actor="user")` for a UI serving a human; default
"agent") — never per call, so nobody can write someone else's name.
Registration-class actions (`register_site`, `site_unregister`,
`site_teardown`) always audit as "user": they are user-confirmed by
doctrine. Embedders multiplexing one workspace across principals scope
attribution with `with w.as_actor("agent:<conversation-id>"): ...` —
a context manager on the object (contextvar-backed, so concurrent
facade threads don't cross), deliberately NOT a tool parameter and not
in `PUBLIC_TOOLS` for the same reason. The actor string is free-form
(hygiene-checked: non-empty, ≤200 chars, no control characters);
`agent:<conversation-id>` is the documented convention, not a
registry.

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
