# Environments

An environment is a **spec**, solved once into a lockfile whose hash is the
**EnvID** — the cache key for everything. Realization (making it usable on
a site) is automatic and strategy-selected per site capability: `prefix`
(pixi install; needs index access), `packed` (built on the controller,
shipped as a CAS blob, unpacked offline — air-gapped compute),
`modules+prefix` (site modules loaded first). musl sites refuse env tasks
with `env.unsatisfiable_on_site` (bare tasks still run).

```python
w.env_ensure({
  "name": "hep-fit",
  "platforms": ["linux-64"],           # add osx-arm64 for the laptop
  "deps": {"conda": ["python =3.12", "root >=6.32"], "pypi": ["zfit ==0.24.*"]},
  "variants": {"linux-64": {"conda": ["cuda-version <=12.4", "cupy"]}},
  "modules": ["espresso/7.2"],         # site-provided software (check first:
                                       #   module_check(site, [...]))
  "env_vars": {"OMP_NUM_THREADS": "{{cpus}}"},
  "system_requirements": {"cuda": "12.4"},   # solve GPU stacks anywhere
})   # → {"env_id": "env:v1:…", "status": "solved"|"cached", "summary": …}
```

- **Layering:** `{"extends": <parent spec_hash>, "deps": {...}}` — whole-spec
  re-solve, new EnvID, cheap build (shared package cache). Never install
  into an existing env.
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
packages, and grade. A query, never a substitution: you decide whether the
instant near-match beats an exact solve.
