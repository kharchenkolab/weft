# Worked patterns

**Workstation offload (S1).** Register the ssh site once → `env_ensure` →
`data_register` inputs → submit with `site` named → relay the plan (first
run stages data + builds env; the second run of anything starts in
seconds) → watch events → read previews → fetch only finals.

**Cluster scan (S2).** Check `site_load("hpc")` (idle CPUs, backlog, ETA
with `resources=`) → submit with `"array": N` and real
`resources.walltime` → follow `array.progress` digests; summarize partial
results mid-flight → on `array.done`, inspect failures (previews carry the
log signature), fix, resubmit just those elements → downstream reduction
task mounts the output refs (0 bytes staged).

**GPU burst (S3).** `env_gpu_hint(site)` for the cuda pin → spec variant
with the GPU packages (force GPU builds: `pytorch-gpu` or build selector)
→ cloud site has hard `budget`; the submit plan + `cloud.launched` events
carry the money — relay them → `site_teardown` when the campaign ends.

**Site-licensed code (S4).** `module_check("hpc", ["espresso/7.2"])` →
spec `modules: [...]` (+ conda pre/post-processing deps) → runs only where
the module exists; placement steers there automatically.

**Compile-from-source.** Toolchain env (`cxx-compiler`, `cmake`, ...);
source tree as an input DataRef; build task declares `outputs: ["build/"]`;
downstream tasks mount the built binary ref. Recompiles memoize; the
artifact chains site-side.

**Exploration → record.** `session_start` on the realized base →
`session_exec` / `session_install` at conversational speed →
`session_snapshot` → re-run the final computation under the snapshot
EnvID → that manifest is the citable record.

**After anything weird.** `doctor()` → `reconcile()` (controller restart)
→ `task_logs` / `site_exec(site, cmd, why=...)` for eyes on the machine →
`env_repair` for corrupt realizations → `site_probe` for capability drift.
