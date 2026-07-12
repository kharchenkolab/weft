# Fabric — Document 05 — Agent Interface

## 1. Shape of the surface

The agent sees Fabric as a small set of tools — naturally expressed as an MCP server, though the same surface works as an in-process function API. The design targets three properties: *compact* (few tools, orthogonal, so the model's planning stays reliable), *token-economical* (every response is summarized/structured; bulk content arrives only by explicit request), and *asynchronous* (no tool call blocks on queues or transfers).

```
sites.list()                      → [{name, kind, health, headline capabilities}]
sites.describe(name)              → capability record (trimmed for tokens)
env.ensure(spec | env_id)         → {env_id, status: cached|solved|failed, summary}
env.status(env_id, site?)         → realizations by site
data.register(path | url)        → {ref, bytes, kind}          # workspace → DataRef
data.describe(ref)                → metadata + locations + preview if cached
task.submit(task)                 → {job_id, placement, plan}   # plan: est. staging bytes, env action, queue guess
task.status(job_id | filter)      → [{job_id, state, since, progress hints}]
task.logs(job_id, tail=100)       → text
task.result(job_id)               → manifest (previews inline)
task.cancel(job_id)               → ack
data.fetch(ref, to_path)          → transfers bulk output to workspace
events.poll(since_cursor)         → [{ts, kind, job_id, payload}]  # the agent's notification feed
site.exec(name, cmd, why)         → guarded diagnostic shell (§5)
```

`task.submit` returns immediately with a *plan* — where it will run, what will be staged (bytes, estimated minutes), whether the environment is a cache hit or a build — which the agent relays for anything consequential. This single design choice does most of the trust-building work: the human sees intended effects before they happen, in plain terms.

## 2. The asynchronous loop

Batch queues make "call tool, wait for answer" untenable, so the contract is submit-and-subscribe. The intended agent pattern:

1. Submit; report the plan and job id to the user; continue the conversation.
2. On each subsequent agent turn, drain `events.poll` (the host app injects a compact digest of new events into the agent's context automatically, so even a user's unrelated message lets the agent notice "the scan finished 10 minutes ago").
3. On terminal events, pull `task.result`, reason over previews, fetch selectively.

Long-running jobs therefore cost zero agent attention between events, and the app's UI — driven by the same event bus — keeps the user independently informed (queue position, transfer progress) without any model in the loop. For array jobs, events include per-element completions batched into digests ("1 240/2 000 elements done, 3 failed — failure previews attached"), which is what lets the agent summarize partial results mid-flight, per scenario S2.

## 3. Error taxonomy

Recovery quality depends on machine-readable causes. Failures carry `(stage, code, detail, hints)`; the taxonomy is small and stable:

```
env.solve_conflict        # unsatisfiable spec → hint: conflicting pins listed
env.realize_failed        # build/unpack error on site → hint: log excerpt, retry-able?
env.unsatisfiable_on_site # e.g., module missing → hint: sites where satisfiable
data.transfer_failed      # method, endpoint pair, resumable?
data.verify_failed        # hash mismatch → auto-retried once, then surfaced
site.unreachable          # backoff schedule included
site.capability_violation # e.g., walltime > partition max → hint: nearest valid ask
sched.rejected | sched.timeout | sched.node_failure
job.nonzero_exit          # + classified log signature when possible (§4)
job.oom | job.walltime_exceeded   # from scheduler accounting → hint: observed peak vs ask
quota.storage | budget.exceeded
```

Each hint is designed against a concrete recovery: `job.oom` includes measured peak RSS so the agent can resubmit with a right-sized ask; `env.solve_conflict` includes the minimal conflicting set so the agent can relax a pin; `env.unsatisfiable_on_site` includes alternative sites so re-placement is one call. This taxonomy is the difference between an agent that retries blindly and one that fixes the actual problem.

## 4. Log intelligence, bounded

Raw cluster logs are long and repetitive. `task.logs` supports `tail`, but the epilogue additionally runs a cheap classifier over the log — regex/signature based, not model based — extracting the probable proximate error (traceback tail, "killed" signatures, MPI abort patterns, missing-file paths) into the manifest's `logs.tail` neighborhood. The heavy interpretation stays with the agent, which is good at reading tracebacks; Fabric's job is only to make sure the *relevant kilobyte* of a 200 MB log is what gets into context.

## 5. Guarded diagnostic shell

Abstractions leak, and when they do the agent needs eyes on the machine: `site.exec(name, cmd, why)`. Guardrails are operational and simple: commands run inside the job-sandbox/Fabric-root scope by default, a required `why` string goes to the audit log alongside the command and output, anything matching a small deny-pattern list (recursive deletes outside the Fabric root, scheduler-admin verbs, and similar foot-guns) requires explicit user confirmation in the UI, and everything is rate-limited per site. The purpose is protecting the user from accidents on their own accounts and keeping shared-system usage polite — not building a hardened boundary, which is out of scope for a single-user tool operating under the user's own identity.

## 6. Consent, budgets, and the audit trail

Three tiers of consequence, three interaction patterns. *Free actions* (status, previews, cache-hit submissions to already-registered sites) execute silently. *Costly actions* (large transfers, long walltimes, first realization of a big environment) execute after the plan is shown, with a per-project policy knob for auto-approval thresholds — e.g., "don't ask below 5 GB / 2 node-hours." *Spending or account-level actions* (cloud instance launches, anything with a real price, registering a new site) always require explicit user confirmation, with cloud budgets enforced as hard per-project caps checked before launch and monitored during runs. The audit log records every remote command, submission, and transfer with timestamps and initiating context (which agent turn, which user click), rendered in the UI as a reviewable activity feed — mundane transparency that pays off the first time a user asks "what exactly did it run on the cluster last night?"

## 7. What the agent's system context should say

The tool API is half the interface; the agent's operating doctrine is the other half. It should be short, roughly: prefer registered sites in placement order unless the user says otherwise; always relay plans for costly actions; treat session environments as scratch and snapshot before recording results (Document 03 §7); on failure, read the structured cause before retrying, and never resubmit an unchanged failing task more than once; keep bulk data remote and reason over previews; when a spec needs a new package, extend the spec — don't `pip install` into a realized environment outside a session. These rules are few enough to hold reliably in context and encode everything the architecture needs from the model's behavior.

## 8. UI integration notes

Every agent-visible object has a UI twin: sites page (capabilities, health, footprint, "remove Fabric from this site" button), environments page (specs, EnvIDs, realization matrix across sites, GC suggestions), jobs page (live states, logs, manifests), data page (workspace ↔ locations view, transfer progress). The agent and the user are peers over the same state — either can initiate, both can see everything — which is the correct posture for a tool operating on the user's own resources and, practically, the thing that makes users comfortable delegating more over time.
