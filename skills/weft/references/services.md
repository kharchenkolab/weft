# Services (endpoint-publishing processes)

The third execution shape, beside batch tasks and kernels: a long-lived
process whose *result is a live endpoint near the data* — a dashboard, a
notebook server, a query API colocated with a big dataset the user should
not have to download.

```python
r = w.service_start("hpc", {
    "command": "python app.py --port $WEFT_PORT",   # MUST bind 127.0.0.1
    "env": env_id,
    "inputs": [{"ref": data_ref, "mount_as": "data/run.h5"}],
    "outputs": ["logs/"],          # optional: harvested on stop
}, ports=[8501], ready_timeout=60)

r["endpoints"]        # [{port, local_port, url}] — reach it at url
w.service_status(sid) # state + endpoints (re-tunnels after a restart)
w.service_stop(sid, collect=True)   # closes tunnels; harvests outputs
```

## What weft guarantees

- **Env, staging, provenance identical to tasks** — a service is a task
  whose lifecycle is "up until stopped".
- **Loopback + tunnel**: the process binds `127.0.0.1` on the *site*;
  weft forwards it to a local port over the existing SSH connection (on
  Slurm, through the login node to the compute node). The tunnel is the
  auth boundary — never bind a public interface.
- **Readiness**: `service.ready` fires when the port actually listens; a
  service that never listens fails with `sched.timeout` **and the log**.
- **Death is reported**: `service.exited` with the log tail (the poller
  watches it exactly like a kernel).
- `$WEFT_PORT` (and `$WEFT_PORTS`) carry the port(s) into the command.

Not a deployment platform: one user, one process, one tunnel, explicit
teardown. Stop what you start.
