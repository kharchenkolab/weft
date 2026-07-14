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
- No biological examples in specs, tests, or docs.
