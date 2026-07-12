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
- **Sessions (interactive):** `session_start(env_id, site)` → scratch clone;
  `session_exec`, `session_install(conda=[...])`; when stable,
  `session_snapshot()` → real EnvID (minimal delta over the base). Nothing
  from a session enters the record — re-run under the snapshot EnvID.
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
- **`weakly_reproducible`** (in `env_status` summaries and provenance):
  true when the spec uses `post_install` — those commands are hashed into
  the identity but their *effects* aren't content-pinned. Prefer real
  dependency layers; keep post_install for the genuinely unpackageable.
- **Toolchains are envs too:** no compiler on site → spec
  `{"deps": {"conda": ["cxx-compiler", "make"]}}`; compile as a task with
  the source tree as an input; downstream tasks run the binary via
  site-side chaining. `${CXX}` etc. are set by activation.
