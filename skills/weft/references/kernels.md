# Kernels (persistent interactive interpreters)

For incremental work — "load the data… now fit… now plot" — where each
step needs the previous step's *interpreter state*. Python, R, and Julia.
No sockets: blocks travel as files over the control channel, so kernels
work through login nodes and survive disconnects.

```python
k = w.kernel_start("beamlab", "python", env_id=env_id)["kernel_id"]
# env must already be realized on the site (error: env.not_realized —
# run any task with it first, even `true`)

r = w.kernel_exec(k, "grid = build_grid(200)")        # waits, returns
r["rc"], r["out"], r["err"], r["artifacts"]           # per-block results
# files a block saves under $WEFT_BLOCK_DIR appear in r["artifacts"]

h = w.kernel_exec(k, "fit = scan(grid)", wait=False)  # long block: async
w.kernel_poll(k, h["block"], timeout=30)              # "running" | "done"
w.kernel_status(k)          # state, current_block, blocks_run, idle_s
w.kernel_transcript(k)      # ordered code + rc + output tails
w.kernel_interrupt(k)       # hung block → finishes with rc 130, state kept
w.kernel_stop(k)
```

## When it dies (native crash, OOM — this happens)

The poller notices and emits **`kernel.died`** with the *killing block*
and a log tail; `kernel_status` shows `died`; further execs return
`sched.node_failure` with the recovery named.

```python
fresh = w.kernel_restart(k, replay="successful")
# starts a NEW kernel — use fresh["kernel_id"] from here on; the old id
# keeps its transcript. replay="successful" re-executes the transcript's
# rc==0 blocks to rebuild state (skip the killer). replay="none" for a
# clean slate.
```

## Doctrine

Kernels are **exploration**. Nothing from a kernel enters the record:
when the analysis stabilizes, assemble the successful blocks
(`kernel_transcript`) into a script and run it as a normal task — that
manifest, with `provenance()`, is the citable result. Stop kernels when
done (`kernel_status`'s `idle_s` tells you which ones you forgot).

## Promoting exploration into the record

Default doctrine stands: for full `"task"`-grade reproducibility, assemble
the successful blocks into a script and run it as a task. But when the
result depended on **accumulated interpreter state** (re-running would be
wasteful, or wouldn't reproduce it), promote instead:

```python
m = w.kernel_promote(k, blocks=[7])     # only successful blocks
m["reproducibility"]   # "transcript" — one rung below "task"
m["transcript"]        # the FULL ordered chain (0..7) that built the state
m["outputs"]           # the blocks' artifacts, content-addressed
```
The result is a first-class manifest: `task_result(m["job_id"])` and
`provenance(...)` work on it, honestly labeled. Nothing is promoted
implicitly.
