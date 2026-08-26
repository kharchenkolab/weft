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
- Pace protocol: TARGETED green (new tests + touched files' suites +
  red-proofs + reality spots) => commit+push immediately; the full
  fast lane runs in BACKGROUND after the push (failures triage per
  the flake ledger; real breakage = immediate fix-forward commit).
  Exception, flagged explicitly: high-blast-radius changes (store/
  engine refactors touching everything) still gate on the full lane.
  The background lane's log is READ before the NEXT round closes —
  a round never closes on an unread lane log, and exit!=0 includes
  COLLECTION errors (exit=2, "N deselected, 1 error": NOTHING ran).
  The kernel_start outage rode exactly this: a duplicate test
  basename aborted every full-lane collection for three commits, the
  would-have-caught test never ran, and the exit=2 logs went unread
  while serial docker lanes occupied attention.
  test_unique_basenames pins the collision class.
- misc/ is gitignored (ledger, designs, specs, handoff);
  misc/HANDOFF.md is the session-to-session index — read it first.
- Test lanes: fast `pixi run pytest -q -m "not solver and not docker"`;
  docker lane `WEFT_SSH_TIMING=10 pixi run pytest -q -m docker`
  (the timing lever prints slow transport ops — slow SUCCESSES
  are invisible to every failure path); solver lane hits real
  indexes.
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
- LANE-FAMILY PARITY: a capability added to ONE lane of a surface
  family (install lanes, evidence persistence, classifiers, URL
  grammars, budgets, policy levers) triggers a family enumeration —
  wire every sibling or record a reasoned n/a in the round. bug5 was
  three cells of a matrix nobody had drawn: the toolchain went to the
  cran lane (its incident), PPM URLs to the solver lanes (their
  incident), and the sibling lanes an agent actually drives got
  neither. Sweeping the DEFECT is not sweeping the CAPABILITY; the
  matrix artifact is misc/lane_parity_2026-08-26.md.
- ADVICE NAMES PULLABLE LEVERS: every hint/remedy names only levers
  reachable from the surface that can hit the raise (a lever with a
  surface-specific spelling names both spellings). The bug5-A2 sweep
  found a refusal advising a kwarg the verb REFUSES at bind time and
  session-only levers printed on realize surfaces — with test_remedies
  PINNING the unreachable word (a test asserting the implementation
  ratifies miscoding: the CODES-registry lesson, for levers).
  test_lever_reachability.py mechanically binds every
  `verb(kwarg=)`-shaped advice string against the live signature;
  surface mismatches are pinned per-remedy.
- Field bug => CLASS SWEEP before closing: generalize the defect and
  sweep the codebase for siblings (subagents; see
  misc/sweep_findings_2026-07.md — 4 field bugs generalized to ~35).
  Fixing instances one at a time is how the same class returns.
  This applies to YOUR OWN misfired edits at any scale: when a bulk
  edit lands wrong, the blast radius is the PATTERN — grep the
  inserted/changed text and audit every hit BEFORE trusting any test
  result (the array_result incident: an unbounded str.replace had
  three landing sites; the fix-forward repaired only the ones failing
  tests illuminated, and the third rode two full green lanes).
  Scripted replaces assert occurrence counts; the Edit tool's
  uniqueness check exists for exactly this.
  Edit shapes that PARSE clean but kill every call of a function get
  AST conformance tests, not process vows: use-before-local-import
  (test_local_import_order — the kernel_start outage rode THREE
  commits because the alias also existed at module level, so the code
  read fine and only DRIVING the verb crashed) joins the _vocab-fold
  parameter test. A fourth instance of an edit family = write the AST
  test for the family, and give the surface's cheapest fast-lane
  DRIVER a place in the touched-file targeted set (the umbrella's
  three consumers had two behavioral tests; the third surface was the
  broken one).
- A green lane certifies ONLY what it covers. Every PUBLIC_TOOLS verb
  needs a FAST-LANE test (docker/solver suites don't gate pushes);
  test_public_tools pins the uncovered-verbs allowlist, which may only
  shrink. Before declaring a fix verified, ask what the lane CANNOT
  see (array_result's only test was docker-marked — the broken verb
  shipped under two green confirmations until a consumer drove it).
- Failure payloads are contracts: a raise with N trigger paths gets N
  tests asserting the DISCRIMINATING fields are true per path (never a
  bare hint whose provenance is ambiguous — install_rc vs verify_rc).
  Pick the error code from the CODES registry MEANING before reading
  the implementation; a test asserting the implementation's code
  ratifies miscoding as spec.
- External tools: new invocation => row in misc/tool_honesty.md +
  compensating check (positive markers over rc-trust).
- One vocabulary, one parser: a string grammar with two implementations
  is a bug in waiting (subdir refs installed in sessions and 404'd in
  solves; same-owner refs collapsed in extends merges). Shared
  vocabularies get ONE owner function; a conformance table drives every
  consumer (all-accept or all-refuse-loudly — accept-and-mangle is the
  tested failure); hand-off emitters (snapshot, adopt, retry) test
  against the REAL consumer, never a mock.
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
- Hostile-ambient-state battery: every surface that executes USER
  CODE inside a weft-owned process (kernel drivers; anything sourced
  into runner.sh — activation, site_prelude) gets a battery mutating
  the process globals the bookkeeping relies on (cwd, stdio, WEFT_*
  env, signals). Cooperative-only corpora ratify the happy path —
  the setwd incident killed kernels with an ordinary idiom while
  every kernel test used clean blocks (field note #5's lesson,
  re-learned on the least-validated input surface).
- A comment claiming a check exists ELSEWHERE must name the test that
  pins that elsewhere. No pinning test => write the check inline, or
  write "UNCOVERED: <lane>" and a report line. A coverage claim
  without a pin is the 2026-08-24 squashfs class: the docstring said
  "checked at the staging prefix inside its own build", no such call
  existed, and the claim stopped everyone — author included — from
  looking; the consumer's published packs (the motivating incident's
  own lane) realized clean around the detection.
- Advisory/hint fields ship with their MOTIVATING INCIDENT replayed
  as a test — the reporter's spec/commands, verbatim where possible.
  A hint matrix that exercises content and the success path but never
  the failure it was built for ratifies decoration (channel_hint
  attached only on solve SUCCESS while its whole scenario — a
  bioconductor spec without bioconda — essentially always FAILS to
  solve; found live by the consumer, same day).
- Ask-driven rounds close by replaying the ask's reported transcript
  as a test where one exists. The incident IS the acceptance test;
  a green round that never ran the reporter's input proved something
  adjacent to the report, not the report.
- Safety checks wired through parameters are FAIL-CLOSED: required,
  never optional-with-a-disarming-default. store=None on the squashfs
  post-link check let the publish lane — the motivating incident's
  own artifacts — silently skip it (consumer audit #3, same week as
  #1 and #2 on the same feature). A forgotten required argument
  crashes at the call; a defaulted one ships dark.
- A conformance test pins the CLAIM's subject. "Every lane routes
  through the check" is a claim about CALLERS: enforce it with a
  required parameter (Python covers every caller) plus a behavioral
  spy driving each entry point — a source-grep of the callee's body
  passed while production disarmed the call through a default.
  Callee-greps only ever pin callees.
- When a fix adds a call or parameter to a shared function, grep ALL
  callers before closing — the misfired-edit blast-radius rule
  applies to FIXES: the pattern is the function's call sites, not the
  one in view.
- DEPLOYMENT SHAPES are a fixture dimension: the configuration
  consumers actually ship must exist as a test fixture, not just the
  configuration development produces. Adopted packs could never be
  snapshotted, on every production install, because every session test
  — weft's AND the consumer's — ran on SOLVED workspaces, where
  env_ensure writes the specs row as a side effect; the adopt-only
  workspace (the entire point of publish/adopt) was never a fixture
  (2026-08-25). test_adopt_only.py's publish-in-A/adopt-in-fresh-B
  fixture is the pattern: hand-off artifacts cross REAL workspace
  boundaries, only site tooling the lane lacks gets stubbed.
- Fixture AXES sit at the shipped shape, not the minimal one: an axis
  pinned simpler than the field norm (single-platform packs when every
  publish is [linux-64, osx-arm64]) makes a whole bug class
  unobservable at ANY test depth — platforms[0] pinning rode months of
  REAL layered-install tests because with one platform "first" and
  "correct" are the same value; the tests could not FALSIFY the claim,
  so their green certified nothing about it. Depth on a degenerate
  fixture is not coverage. When two axis values behave differently,
  the fixture encodes the difference (per-platform build strings), so
  cross-contamination cannot pass by coincidence.
- WIDENING an input is a class-sweep trigger: a round that grows a
  field's reachable range (new default, newly inherited value,
  loosened validation) audits every downstream consumer of that field
  against the new range BEFORE shipping — grep the field name, re-read
  each use. The #117 platforms-from-parent declaration was correct in
  isolation; it activated a latent platforms[0] two functions
  downstream, and the round's tests all exercised the OLD range.
- Remedy text is CODE: every reusable suggestion/note lives in
  remedies.py as a function taking the facts it discriminates on,
  gated on a MARKER of the actual cause — never pasted prose at the
  raise site. The extends_env shut door was pasted at FOUR sites; the
  #118 sweep fixed two and certified "both" (prose drifts past greps;
  a registry makes a new landing site a call and a copy impossible).
  A remedy with N applicability paths gets N tests, each replaying
  the misdirection it prevents.
- Evidence outlives the process: any operation with timeout >= 300s
  persists FULL output (evidence.run_logged site-side; the lock.py
  solve.err pattern controller-side); payload tails are WINDOWS onto
  a persisted log (log_path), never the only copy; error_regions are
  marker-anchored because the causal line is positionally anywhere
  (R prints dep-availability before the first download). Discarding
  output on rc!=0 is a defect class (ensure_toolchain shipped it).
  Failure payloads on build lanes carry log_path + error_regions +
  tail, and the durable record keeps them too.
- Path inputs declare a REALM (controller|site) in ONE normalize
  owner; _site_realm_values is the single enumeration of path-shaped
  config keys (adding one means adding it there). Site-realm '~'
  resolves against the SITE's home at registration and is STORED
  absolute (shlex.quote suppresses shell tilde-expansion — an
  unresolved '~' is a literal dirname until rattler panics);
  controller-side expanduser is the wrong machine for remote sites.
  Stored pre-fix rows refuse typed at adapter construction.
- The tool boundary is ONE owner: the `tool` wrapper converts
  everything typed (WeftError; binding TypeError -> tool.
  bad_arguments with the live signature, bind-discriminated from
  body TypeErrors; crashes -> internal.error + event), _seal
  enforces envelope JSON-serializability (strict suite-wide via
  conftest — the ensure_available cycle rode the RETURN path of
  green-tested code), and a second parser of the same boundary in a
  consumer-facing layer (mcp's TypeError arm) is the
  two-implementations bug. Dict-shaped params carry SCHEMA_HINTS
  (ratchet-pinned) — the shape belongs in the schema an agent SEES,
  not only the docstring.
- Hardcoded operational constants an agent can hit are missing
  LEVERS: timeouts and output caps take bounded parameters
  (_bounded refuses out-of-range with the bounds named — a silent
  clamp falsifies the run's numbers). The sanctioned diagnostic
  path must be at least as capable as the raw-ssh workaround, or
  agents will (rightly) leave the audited surface.
- Probe endpoints are load-bearing: capability checks hit CDN-cached
  STATIC objects, never dynamic roots (shim v13 — anaconda.org root
  measured 3.6-8s+ under load and intermittently mislabeled
  networked sites air-gapped, a rotating-member test flake).
- Lane debt is round debt: a round that touches realize/session/
  publish/kernel paths runs the docker AND solver lanes at round end
  — SERIALLY, never concurrently (three parallel lanes on one mac
  produced four timing false-failures) — and triages them before the
  round's task closes. Twelve rounds of deferral piled 13
  simultaneous failures with tangled attribution: two real bugs, two
  world-drift breaks, stale contracts, and flakes all looked alike
  until each captured output was read.
- Activation is offline by contract: every `pixi shell-hook` runs
  --frozen (an unfrozen hook re-checks the lock against live
  repodata — a hidden NETWORK dependency at activation time that
  broke file://-channel envs and air-gapped sites; five call sites,
  none frozen, found in one R1 sweep). A new hook call site copies
  the flag or breaks the chaos lane.
- A refusal NAMES ITS SUBJECT: when the discriminating subject
  (package, file, site, ref, key) is in scope at the raise, it goes
  in detail or hints — and distinct inputs must produce
  DISTINGUISHABLE payloads. 124 distinct wrong cran names produced
  124 byte-identical refusals and a three-hypothesis misdiagnosis
  (aba2 ask 32) while the R resolver was already printing the
  missing set to stderr; the subject sweep (misc/subject_sweep) found
  ~80 sibling sites. Tests assert the subject appears in the payload,
  not merely the error code.
- Solver/tool stderr classification runs on a CAPTURED CORPUS
  (tests/fixtures/stderr_corpus/): real output, one file per shape,
  append the verbatim stderr of every misclassification incident.
  Author-written classifier fixtures carry exactly one marker each
  and cannot express overlapping-marker theft (ask 31: the parse arm
  matched any pixi.toml:N:M span and stole resolve errors into
  internal.error 'do not edit pins'); arm ORDER is load-bearing and
  the corpus is what pins it.
- Repeat-call semantics are a test SHAPE: any record that can be
  written twice for one key gets a second-call test asserting
  accumulate-vs-replace explicitly (retained selections accumulated
  in the keep dir but the row REPLACED — and discard then deleted
  the first retain's promised-safe files; one retain per test was
  the only shape ever exercised).
- No biological examples in specs, tests, or docs.
