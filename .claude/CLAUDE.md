# weft — aims, principles, conventions

Execution substrate for agent-driven scientific analysis: reproducible
environments (content-addressed EnvIDs), content-addressed data, and an
async task/kernel/service model over the user's own machines (laptop →
SSH → Slurm → cloud). Everything userspace: no root, no daemons on
remotes, removable in one command.

## Principles

1. **Honest numbers, honest failures.** Never report what wasn't
   measured ("unknown" ≠ "unlimited"). Every failure is structured —
   {error, stage, detail, hints, retryable} — with remediation an agent
   can act on.
2. **Agent-adjustable by design.** Sites are weird; weft will meet
   broken schedulers, submit filters, shadowed PATHs. Every failure an
   agent can SEE must map to a lever it can PULL without a weft code
   change: capabilities_override, prefer, policy, scheduler extra
   directives, modules_init/site preludes, site_note, site_exec(why=).
   When adding a feature that generates or probes, add its override.
3. **Identity is content.** EnvID = lock hash; memoization = task hash;
   names/labels/versions are display pointers, never identity, and
   never perturb caches.
4. **Shared things are immutable.** Published/read-only envs never
   change in place — new version alongside, pointer flip, grace period.
5. **Fixtures lie; reality is the test.** Parsers and probes get
   validated against real clusters; quirks are recorded via site_note
   and the ledger, then become fixture cases.

## Conventions

- Commits: short, imperative, NO AI signatures or generated-with
  footers. Push at round ends or when asked.
- Per-round OODA: tests first-run where possible; docs
  (documentation/) and the agent skill (skills/weft/) updated with
  every surface change; round entry in misc/report.md.
- misc/ is gitignored (ledger, designs, specs, handoff);
  misc/HANDOFF.md is the session-to-session index — read it first.
- Test lanes: fast `pixi run pytest -q -m "not solver and not docker"`;
  docker lane needs a container runtime; solver lane hits real indexes.
  (On the current mac: PYTHONNOUSERSITE=1, docker = OrbStack.)
- Reality matrix is per-PROTOCOL, not per-site: validating a site (or
  changing a protocol) covers every job kind — env, task, kernel
  cadence, array, service. Kernels shipped 5 rounds without one real
  remote block (bug2); transport quirks are per-protocol.
- Machine-cadence rule: any surface an agent drives gets one test that
  drives it as fast as the API allows (test_kernel_block_race.py,
  test_agent_cadence.py). Timing-dependent ssh tests use the
  sshd_site_wan (50ms netem) fixture — loopback timing hides races.
- Polled files: adding one to any protocol requires a row in
  misc/polled_files_audit.md; consume-once readers require an atomic
  writer + a conformance case.
- Flakes are evidence: before retrying/quarantining a flaky distributed
  test, write a one-line hypothesis in the ledger (percent-level races
  look exactly like flakes) — and READ the captured failure output
  before writing the hypothesis.
- Field bug => CLASS SWEEP before closing: generalize the defect and
  sweep the codebase for siblings (subagents; see
  misc/sweep_findings_2026-07.md — 4 field bugs generalized to ~35).
  Fixing instances one at a time is how the same class returns.
- Failure payloads are contracts: a raise with N trigger paths gets N
  tests asserting the DISCRIMINATING fields are true per path (never a
  bare hint whose provenance is ambiguous — install_rc vs verify_rc).
  Pick the error code from the CODES registry MEANING before reading
  the implementation; a test asserting the implementation's code
  ratifies miscoding as spec.
- External tools: new invocation => row in misc/tool_honesty.md +
  compensating check (positive markers over rc-trust).
- Malformed input is a test lane: intake boundaries (spec from_dict,
  verb list args) get hostile cases — duplicates, case collisions,
  container-breaking strings — asserting the REFUSAL payload; anything
  weft renders for an external parser (TOML/TSV/R/shell) gets the pair
  (refuse at intake; internal.error, no pin advice, if it still fails
  to parse). Callers are generators with their own bugs; testing only
  author-written inputs ratifies the happy path (field note #5: a
  spliced duplicate key rode a valid-input-only test suite straight
  into env.solve_conflict).
- Computed defaults get property tests (concrete, UTC-derived,
  published); never derive a default from local wall-clock for a
  resource keyed on someone else's clock. Cross-clock comparisons
  (FS-server vs node vs controller) need explicit margins.
- Reality runs sweep VERBS, not just the demo path: validating feature
  F on topology T drives every mutation verb of F (a session reality =
  start+exec+INSTALL+snapshot+stop). "Read works" says nothing about
  the extend path (cold-base session finding).
- Every "cheap because X" design premise names what happens when NOT-X,
  and either a test pins the not-X behavior or a runtime guard detects
  it (session clone was "cheap because warm cache" — adopted packs are
  never warm).
- Reality scripts assert COST budgets (bytes moved, seconds), not just
  correctness — a 1.6 GB re-download looks green without them.
- No biological examples in specs, tests, or docs.
