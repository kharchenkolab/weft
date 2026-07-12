# Fabric — Document 06 — Roadmap, Testing, Risks

## 1. Phasing

The phases are ordered so that each one delivers standalone user value and de-risks the next, and so that the hardest *novel* piece — the spec → EnvID → realization model — is forced to prove itself in phase 1 on the simplest possible transport.

**Phase 0 — Extract and harden the local substrate (2–3 weeks).** Refactor the app's current local execution into Fabric's shapes without any remote capability: EnvSpecs and lockfile-hashed environments replace ad-hoc layering locally; DataRefs and manifests replace loose files; the tool API replaces direct calls. Acceptance: the app runs entirely through Fabric on one machine, existing analyses unchanged from the user's perspective, and every local run now produces a manifest and provenance record. This phase looks like pure refactoring but is where the object model gets debugged cheaply.

**Phase 1 — SSH sites (3–5 weeks).** Bootstrap protocol, shim, capability probe, `ssh` adapter with detached execution, `rsync-ssh` transfers, `prefix` realization, session environments with snapshot. Acceptance: scenario S1 end to end — including the second-run experience (env cache hit + data cache hit → start within seconds), laptop-sleep survival, and a crash-recovery test (kill Fabric mid-job, restart, reconcile correctly).

**Phase 2 — Slurm (4–6 weeks).** Scheduler adapter (submit/poll/cancel via login node, batched polling), array tasks, `packed` and `container` (Apptainer) realizations with the strategy selector, compute-vs-login probe split, scratch-purge resilience, module support (`modules+prefix`). Acceptance: scenarios S2 and S4 on a real university-style cluster, plus the full matrix on the CI cluster (§2): every realization strategy × array/non-array × shared/non-shared staging paths.

**Phase 3 — Cloud via SkyPilot (3–4 weeks).** `cloud` adapter wrapping SkyPilot task translation and cluster lifecycle, object-store cache backing, budget enforcement, `container` realization as default. Acceptance: scenario S3 with hard budget caps demonstrated (launch refused over cap; runaway-cost watchdog fires in a simulated overrun).

**Phase 4 — Breadth and polish (ongoing).** Globus transfer method, then optionally the Globus Compute adapter; PBS/LSF via the scheduler-verb abstraction or PSI/J; placement scoring improvements from accumulated timing data; provenance export; multi-hop relay staging. Ordered by observed user demand, not speculation.

A deliberate sequencing note: MCP-Slurm wrappers already exist in the wild and could ship "agent submits cluster jobs" in a week — but without the environment and data model they produce exactly the brittle, non-reproducible behavior this design exists to prevent. The phasing resists that shortcut: transport is phase-1 easy; the model is the product.

## 2. Testing strategy

Three layers. *Unit*: the pure cores — spec merge algebra, lockfile canonicalization and hashing, strategy selection table, staging-plan set arithmetic, batch-script rendering — are all pure functions and get exhaustive table-driven tests. *Adapter conformance*: a single conformance suite runs against every adapter (the `local` adapter is the oracle) asserting identical sandbox layout, manifest content, and lifecycle event sequences for a canonical task set; new adapters must pass it before merge. *Integration*: containerized fixtures in CI — an `sshd` container for the SSH adapter, a slurm-in-docker cluster (multiple community images exist) for the scheduler adapter, and a MinIO container standing in for object stores; nightly (not per-commit) runs exercise a real cloud smoke test behind a spend cap. Chaos cases get first-class fixtures because they are the actual product surface: dropped SSH mid-job, scratch purge between staging and run, queue rejection, OOM kill, Fabric crash at each lifecycle stage.

A fourth layer is unusual but essential here: *agent-in-the-loop evaluation*. A scripted evaluation harness replays recorded analysis scenarios (S1–S4 plus failure variants) against a live agent with the Fabric tools, scoring task success, unnecessary retries, plan-relay compliance, and token cost. This catches interface regressions that unit tests cannot — e.g., an error hint whose wording reliably sends the model down the wrong recovery path.

## 3. Metrics of success

Cold-to-warm ratio: second submission of an identical task should start ≥ 50× faster than the first on SSH sites (cache hits all the way down). Bandwidth honesty: bytes moved per project week should trend toward the size of *new* data only. Recovery rate: fraction of failed jobs the agent remediates without user intervention, by taxonomy code. Footprint auditability: a user can list and remove everything Fabric placed on a site in one action. And the product-level metric: fraction of analysis tasks users choose to route off-desktop, which measures earned trust.

## 4. Risks and mitigations

*Environment realism.* Some scientific software resists conda-forge packaging or needs site-tuned builds (vendor MPI, exotic interconnect libs). Mitigations are layered by design: `modules` for site-provided stacks, `container_base` for system-library needs, source-build-as-task-input for project codes (Document 03 §8), `post_install` as the honest escape hatch. Residual risk is real but bounded to a minority of specs, and the model degrades gracefully (weaker reproducibility labeling, not failure).

*Login-node etiquette and site policies.* Shared facilities have rules about login-node processes and polling. Mitigations: batched polling, connection multiplexing, rate limits, no remote daemons, and a per-site policy block the user can tighten. Worst case, a facility's rules push us to the Globus Compute adapter earlier for that site.

*Heterogeneous quirks long tail.* Every cluster is a snowflake (module system dialects, scratch policies, arch mixes). The capability record + strategy table localizes quirk handling to data and small probes; the guarded shell gives the agent a diagnostic path when the model misses; and the conformance suite prevents quirk fixes from regressing other adapters.

*Dependency bets.* pixi and SkyPilot are healthy, fast-moving projects; both are wrapped behind Fabric interfaces (`Realizer`, `SiteAdapter`) sized so that replacing either is weeks, not months. Lockfile format churn is mitigated by storing our own canonicalized form alongside the native file.

*Scope creep toward workflow management.* The agent will eventually want DAGs. The firewall is architectural: Fabric's task memoization and site-side output chaining (Document 04 §3) give the agent most of what a DAG engine provides while Fabric itself stays a task executor. If a real workflow layer is ever warranted, it composes *on top of* the tool API.

## 5. Open questions

Where should resolution run when the laptop is offline but sites are reachable — promote a designated site to "solver host"? Should EnvIDs incorporate a schema version so future canonicalization changes don't orphan caches (proposal: yes, `env:v1:<sha256>`)? How much of the placement score should be learned from history versus configured — and what is the minimal telemetry (purely local) needed to learn it? Is per-element manifest granularity right for very large arrays (10⁵ elements), or do we need hierarchical roll-ups in the manifest format itself? And finally naming: "Fabric" collides with the Python SSH library of the same name, so a real name is needed before anything public.

## 6. Summary

The proposal in one paragraph: keep the agent above a four-part abstraction — sites described by probed capabilities, environments as content-addressed resolutions of declarative specs realized per-site by a capability-driven strategy, data as content-addressed refs with location tracking and lazy result retrieval, and a compact asynchronous tool API with structured errors and plan-before-effect consent. Build it thin over pixi, SSH/rsync, Slurm, and SkyPilot; prove the model locally and over SSH first; let clusters and clouds be adapters rather than architectures. The result is a modular unit that turns "the user has heterogeneous compute somewhere" into "the agent can use it, reproducibly, with a paper trail."
