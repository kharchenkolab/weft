# Environments

An environment is a **spec**, solved once into a lockfile whose hash is the
**EnvID** — the cache key for everything. Realization (making it usable on
a site) is automatic and strategy-selected per site capability: `prefix`
(pixi install; needs index access), `packed` (built on the controller,
shipped as a CAS blob, unpacked offline — air-gapped compute),
`squashfs` (the env as ONE mounted image — auto on parallel-FS roots
like BeeGFS/Lustre where per-file metadata is the recurring cost; force
it with site config `prefer: "squashfs"` for read-only institutional
envs on NFS; jobs mount it in a private per-job namespace where userns
exists), `modules+prefix`/`modules+squashfs` (site modules loaded
first). musl sites refuse env tasks with `env.unsatisfiable_on_site`
(bare tasks still run).

```python
w.env_ensure({
  "name": "hep-fit",
  "platforms": ["linux-64"],           # omitted → the controller's own
                                       # platform; declare explicitly to
                                       # target foreign-platform sites
                                       # (else env.platform_mismatch names
                                       # the missing one)
  "deps": {"conda": ["python =3.12", "root >=6.32"], "pypi": ["zfit ==0.24.*"]},
  "variants": {"linux-64": {"conda": ["cuda-version <=12.4", "cupy"]}},
  "modules": ["espresso/7.2"],         # site-provided software (check first:
                                       #   module_check(site, [...]))
  "env_vars": {"OMP_NUM_THREADS": "{{cpus}}"},
  "system_requirements": {"cuda": "12.4"},   # solve GPU stacks anywhere
})   # → {"env_id": "env:v1:…", "status": "solved"|"cached", "summary": …}
```

## Published envs (institutional read-only bases)

Admins publish; lab members adopt BY NAME and extend — nobody re-solves:

```python
# admin (audited as user; tree must live OUTSIDE any weft root):
w.env_publish(env_id, "hpc", "/groups/lab/weft-base",
              name="lab-py", version="2026.07")
# consumer (site registered with ro_roots=["/groups/lab/weft-base"]):
env = w.env_adopt("hpc", "/groups/lab/weft-base", "lab-py")["env_id"]
w.task_submit({..., "env": env})                  # adopted in place, RO
mine = w.env_ensure({"name": "mine", "extends_env": env,
                     "platforms": ["linux-64"],   # match the published env!
                     "deps": {"pypi": ["emcee"]}})  # overlay on the RO base
w.env_published("hpc", "/groups/lab/weft-base")   # render-ready rows
w.env_unpublish("hpc", tree, "lab-py", "2026.07") # pointer only; grace
                                                  # period; purge=True deletes
```

- `env_published` rows are complete enough to render directly
  (`published:v1`): per version the catalog's write-time facts (grade,
  spec_summary, glibc_floor, image bytes) plus this workspace's
  read-time truth — `is_latest`, `runnable_here` (None when the site's
  glibc is unknown), `state_here`
  (adopted-ro/ready/building/failed/missing), `last_used`.
- Variant publishes (e.g. an old-glibc build of the same release) pass
  `latest=False` so they don't hijack the default pointer.
- On userns sites the build churn lands in a STAGING dir (bind-mounted
  at the tree path per build command — baked paths stay the tree's);
  the tree gets one sequential image write. Vital for slow netfs trees.
  Levers: `env_publish(..., staging="auto"|"/fast/dir"|"none")`, site
  config `publish_staging`. The result's `staging` field says what
  actually happened (a live probe gates; no userns → honest fallback
  to build-at-destination).

- `env_packages(env_id, platform=None)` lists the RESOLVED records
  wholesale — name, version, ecosystem (conda/pypi/cran/…), platform —
  from the stored lock; one read, no solve. (`env_why` stays the
  per-name explainer.) Layer records carry platform=None: source
  releases, resolved per site at realize time.
- Adoption reads the catalog's stored lock — NO solving (re-solving
  decays as the index moves and would silently rebuild privately).
- Versions are catalog pointers over immutable content-addressed dirs;
  upgrades publish alongside and flip `latest` — never in place (jobs
  may be running against the mount; overlays pin exact parent EnvIDs).
- The base is filesystem-enforced read-only (EROFS on write); consumer
  jobs mount it in private per-job namespaces where userns exists.
- extends_env children must declare the published env's platforms when
  your controller's platform differs (e.g. mac laptop → linux cluster).

- **Layering, two flavors — never install into an existing env:**
  - `{"extends": <parent SPEC hash>, "deps": {...}}` — whole-spec
    re-solve. The base may move. New EnvID, cheap build (shared cache).
  - `{"extends_env": <parent EnvID>, "deps": {...}}` — **freeze the base**:
    every package in the parent's lock becomes an exact pin (conda, pypi,
    cran/julia layers including github SHAs and the parent's snapshot
    date), so the child's lock is a superset *by construction*. The solve
    is fast (tiny search space), and if the delta is pure language-layer
    (`deps.pypi` / `deps.cran` / `deps.julia`), the child **realizes as an
    O(delta) overlay** on the parent's already-realized prefix: seconds and
    megabytes for "the same env plus one package". A conda delta, an extras
    delta (modules/post_install), or a delta contradicting a frozen pin
    can't overlay — the response's `delta.why` explains, and the same
    EnvID just realizes as a full prefix (identical behavior, more disk).
    A contradiction raises `env.layer_conflict` naming the package and the
    lever (`extends` for a free re-solve). This is the right tool
    mid-analysis: "add emcee to what I have" without re-solving the world.
- **Re-solve only on request:** `env_ensure(spec, update=True)` picks up new
  channel state; the old EnvID stays valid for reproducing old results.
- **GPU:** `env_gpu_hint(site)` reads the probed driver and returns the
  `cuda-version` pin + note. Packages with CPU/GPU builds need the GPU
  variant forced: `pytorch-gpu` metapackage or `"pytorch 2.* *cuda*"`
  build selector. Apple Silicon needs nothing (MPS is in default builds).
- **Sessions (interactive):** `session_start(spec_or_env_id, site)` → a
  mutable scratch clone (one call: it realizes the base for you);
  `session_exec`, `session_install(conda=[...])`,
  `session_run_installer(cmd, note=...)`; when it stabilizes,
  `session_snapshot(notes=[...])` → a real EnvID carrying whatever you did.
  Iterate freely here — that's what it's for. See "Adaptive moves" below.
- **Reuse:** identical resolutions share EnvIDs; realizations re-adopt
  across workspaces from the site marker; `env_status(env_id)` shows the
  per-site realization matrix (your memory of what is installed where).
- **Integrity & repair:** every job start re-checks the realized env's
  executable inventory against its build-time fingerprint — a tampered or
  partially purged env rebuilds automatically (`realize.integrity_failed`
  event) instead of silently falling through to host binaries. For damage
  that check can't see (corrupted file *contents*), the symptom is wrong
  results or import errors: `env_repair(env_id, site)` clears the
  realization and the next task rebuilds from the lockfile.
- **Identity forms:** conda/pypi-only envs are `env:v1:<hash>`; envs with
  extra layers (cran, …) are `env:v2:<hash>`. Both are content-addressed
  cache keys; the version only reflects the lock document's shape.
- **Reproducibility is graded, not binary.** `env_status` and every
  manifest carry `reproducibility` plus the per-component breakdown:

  | grade | means |
  |---|---|
  | `fully-pinned` | every package content-hashed; re-materializes exactly |
  | `snapshot-pinned` | dated snapshots / commit SHAs (CRAN, GitHub, Julia) — reproduces almost always, artifacts not content-hashed |
  | `attested` | uses site `modules` (or the bare site env) weft cannot pin |
  | `escape-hatch` | a `post_install` / session installer ran; effects not content-pinned |
  | `state-dependent` | kernel-promoted from interpreter state; replay the transcript |

  The grade is the *worst* rung any component earns; the components tell
  you which step is the soft one. **This is information, not a warning.**
  An adaptive step that unblocks real work is the right call — take it,
  and say why in `notes` (spec-level) or `step_notes` (per post_install
  index). Notes are **excluded from the EnvID hash**, so annotating costs
  nothing: no forked identity, no orphaned caches.
- **Toolchains are envs too:** no compiler on site → spec
  `{"deps": {"conda": ["cxx-compiler", "make"]}}`; compile as a task with
  the source tree as an input; downstream tasks run the binary via
  site-side chaining. `${CXX}` etc. are set by activation.

## Julia (and the solver registry generally)

`deps.julia` works like `deps.cran`: `["DataFrames", "Example ==0.5.5",
"owner/Repo.jl@ref"]`, requiring `julia` in `deps.conda`
(`env.layer_conflict` tells you if you forget). The lock is Julia's own
Manifest.toml — every package pinned by content tree-hash, github refs by
commit. Realization runs `Pkg.instantiate` against a shared per-site depot
(network needed at install, like cran). Other ecosystems join the same
way — unknown `deps` keys fail fast listing what's registered.

## Adaptive moves (they are normal — use them)

**One-call forgiving solve.** Mark a constraint SOFT with a trailing `?`
and let weft relax it instead of handing you a conflict to hand-resolve:

```python
w.env_ensure({"deps": {"conda": ["python =3.12", "scipy ==1.14.1?"]}},
             relax="soft")
# → {"env_id": …, "relaxed": [{"dep": "scipy ==1.14.1", "got": "1.15.2"}], …}
```
Hard pins are **never** relaxed (a silent version drop is exactly what a
substrate must not do). The result is still fully-pinned: adaptiveness was
in the *path* to a solve, not in what you got.

**Cheap iteration.** `session_start(spec_or_env_id, site)` takes a spec and
realizes the base itself — exploration costs one call. `session_install`
for packages; `session_run_installer(cmd, note="why", source=<path>)` for
the bespoke fix no index expresses (an R `install.packages`, `pip install
-e`, a vendored `make install`).

**Pass `source=` whenever the command needs local files.** weft
content-addresses them into the env (`post_install_inputs`), so the step
travels and the env rebuilds *anywhere*. Without it you get an env that
builds on your machine and nowhere else — `session_snapshot` lints for
this and warns (`portability_warning`), and the grade's `post_install`
component reports `portable: false`.

`session_snapshot(notes=[...])` carries installers into the spec as labeled
`post_install` steps (grade: `escape-hatch`) and, by default, **verifies**
the minted env by realizing it — a citable EnvID that cannot be rebuilt is
worse than an error.

**Drift.** `env_revise(env_id)` when a recorded env can no longer be built:
if a fresh solve reproduces the same identity, the stale lock is re-derived
(nothing changes); if the world genuinely moved, you get a **new** EnvID
plus a package-level diff — the old one is never silently redefined. Sites
can do this automatically with policy `on_drift: "revise"`; the default is
still to fail with a cause.

**Warm near-matches.** `env_find_near(spec, site="hpc")` lists already-
realized envs close to what you want, with their distance, missing
packages, **version mismatches** (a present-but-wrong-version package
counts against the match and is shown, not hidden), and grade. A query,
never a substitution: you decide whether the instant near-match beats an
exact solve.

## Overlay mechanics (what actually happens)

When a child `extends_env` a parent that is realized on the site, the
child env dir holds ONLY its delta: `pylib/` (pip `--target`, artifact
hashes from the lock), `rlib/` (R delta, `.libPaths`-composed), or a
Julia project instantiated against the shared depot. Its `activate.sh`
sources the parent's and appends `PYTHONPATH`/`R_LIBS`/`JULIA_PROJECT`
lines. Same EnvID, same behavior — a conformance test holds overlay and
full-prefix realizations to byte-identical task outputs.

- **Source builds** (github R packages, sdists) compile with a weft-owned
  build toolchain (never added to YOUR env), against the parent's
  headers/libs; artifacts are cached by (source SHA, parent EnvID,
  platform, resolved toolchain lock) — an `env_repair` + rebuild, or a
  colleague on the same site, pays an untar, not a compile
  (`overlay.compile_cache_hit`).
- **Verification gate:** after composing, every delta package is loaded
  (R: `library()`; python: metadata visibility by distribution name) plus
  a sample of the parent's — shadowing or ABI trouble triggers
  `realize.overlay_fallback` and an automatic full build. You never keep a
  broken overlay.
- **Integrity is two-deep:** the child's marker records the parent's
  executable fingerprint; a repaired/tampered/evicted parent rebuilds the
  child on next use (`realize.parent_changed`).
- **Eviction:** `env_evict(parent)` refuses while overlay children are
  realized (`env.evict_blocked`, dependents listed); `cascade=True` evicts
  them with it (all rebuild cache-warm). Evicting a child alone is always
  fine — it owns only its delta. With the parent gone, the child's next
  realization is a full prefix: honest, automatic.
- **Air-gapped sites don't overlay** (delta installs need index access) —
  they get packed/archive realization of the same EnvID.

## R: beyond the base mirror (extra + release-pinned repositories)

The R layer resolves against a dated base-mirror snapshot BY DEFAULT; a
spec can widen the universe, with identity pinned the same way:

```python
{"deps": {"conda": ["r-base =4.4"], "cran": ["somePkg"]},
 # CRAN-like repos resolved JOINTLY with the base mirror (r-universe,
 # drat, institutional mirrors — a package here may depend on base-mirror
 # packages and vice versa); part of the EnvID
 "r_repositories": ["https://<org>.r-universe.dev"],
 # curated repos versioned by a RELEASE LINE: the provider expands the
 # release id to its repo set (companion repos included) and its required
 # R version — weft validates release ↔ r-base upfront
 # (env.layer_conflict names the release, the required R, and the fix)
 "r_release_repos": [{"provider": "<name>", "release": "3.20"}]}
```

Providers are a registry (like solvers/fetchers/transfers): hosts register
`weft.solvers.register_release_repo_provider(name, fn)` where
`fn(release) -> {"repos": [urls], "r_version": "4.4"|None}`. A release IS
a snapshot — same grade rung (`snapshot-pinned`), same EnvID discipline
(two release lines are two envs even when the package sets coincide,
because what was ASKED differs). Everything downstream composes
unchanged: records carry the repo that served each package, `pack_layer`
downloads from it for air-gapped delivery, and `extends_env` children
inherit the whole repo universe (their pins could not resolve otherwise).
