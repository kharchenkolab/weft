"""Service tasks (integration review A1): the third lifecycle.

A service is a supervised, detached process whose result is a **live
endpoint near the data**, not a file artifact — dashboards, notebook
servers, colocated query APIs. It shares the lease machinery kernels use
(poller-watched, death → `service.exited` with the log tail, walltime-
bounded, reconcilable) plus two service-specific pieces:

  * services bind 127.0.0.1 ON THE SITE only; the controller reaches them
    through an SSH tunnel over the existing multiplexed connection — the
    tunnel IS the auth boundary (no public binds, ever);
  * `service_stop(collect=True)` harvests declared outputs into a normal
    manifest, connecting a service's side-products to the record.

Not a deployment platform: one user, one process, one tunnel, explicit
teardown.
"""

from __future__ import annotations

import shlex
import socket
import subprocess
import time
import uuid

from .errors import WeftError

READY_TIMEOUT_S = 60.0


def _free_local_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServiceManager:
    def __init__(self, store, adapters, runner, dataman):
        self.store = store
        self.adapters = adapters
        self.runner = runner
        self.dataman = dataman
        self._tunnels: dict[str, list[dict]] = {}  # service_id -> tunnels

    def _adapter(self, site: str):
        if site not in self.adapters:
            raise WeftError("task.invalid", f"unknown site: {site}",
                            stage="infra",
                            hints={"registered": sorted(self.adapters)})
        return self.adapters[site]

    def _get(self, service_id: str) -> dict:
        s = self.store.get_service(service_id)
        if not s:
            raise WeftError("task.invalid", f"unknown service: {service_id}",
                            stage="infra")
        return s

    # -- lifecycle ------------------------------------------------------------

    def start(self, site: str, task: dict, ports: list[int],
              ready_timeout: float = READY_TIMEOUT_S) -> dict:
        from .task import Task
        if not ports:
            raise WeftError("task.invalid", "a service needs ports=[...]",
                            stage="submit",
                            hints={"note": "bind 127.0.0.1 on the site; "
                                           "$WEFT_PORT carries ports[0]"})
        adapter = self._adapter(site)
        t = Task.from_dict({**task, "site": site})
        service_id = "svc_" + uuid.uuid4().hex[:10]
        jobdir_rel = f"services/{service_id}"

        activate_line, spec_vars = self.runner._ensure_env_for(t, site)
        if t.required_refs():
            self.dataman.ensure_at(t.required_refs(), adapter,
                                   self.runner.transfers)
        env_vars = {**t.env_vars, "WEFT_PORT": str(ports[0]),
                    "WEFT_PORTS": ",".join(map(str, ports))}
        t = Task.from_dict({**t.to_dict(), "env_vars": env_vars})
        self.runner._prepare_sandbox(adapter, jobdir_rel, t, service_id,
                                     activate_line, spec_vars)
        handle = adapter.submit(jobdir_rel, t.to_dict())
        self.store.put_service(service_id, site, jobdir_rel, handle, ports,
                               t.to_dict())
        self.store.emit("service.started", service=service_id, site=site,
                        ports=ports)
        from .poller import Watch
        self.runner.poller_for(site).register(Watch(
            job_id=service_id, handle=handle, jobdir_rel=jobdir_rel, task=t,
            started_at=time.time(), scheduler=False, lease="service"))

        deadline = time.time() + ready_timeout
        while time.time() < deadline:
            if self._port_listening(adapter, ports[0]):
                break
            st = self.store.get_service(service_id)
            if st["state"] == "exited":
                raise WeftError(
                    "job.nonzero_exit",
                    "service exited before becoming ready",
                    stage="running",
                    hints={"log_tail": self.runner.tail_log(
                        adapter, jobdir_rel, 40)})
            time.sleep(0.4)
        else:
            self.stop(service_id)
            raise WeftError(
                "sched.timeout",
                f"service did not listen on port {ports[0]} within "
                f"{ready_timeout:.0f}s",
                stage="running",
                hints={"log_tail": self.runner.tail_log(adapter, jobdir_rel, 40),
                       "suggestion": "does the command bind 127.0.0.1 on "
                                     "$WEFT_PORT? raise ready_timeout for "
                                     "slow starts"})
        self.store.update_service(service_id, state="ready")
        endpoints = self._establish_endpoints(service_id)
        self.store.emit("service.ready", service=service_id, site=site,
                        endpoints=endpoints)
        return {"service_id": service_id, "site": site, "state": "ready",
                "endpoints": endpoints,
                "note": "loopback-bound on site; reach it via these local "
                        "endpoints (tunnel is the auth boundary). "
                        "service_stop when done."}

    def _port_listening(self, adapter, port: int) -> bool:
        hexport = format(port, "04X")
        r = adapter.run_activated(
            f"(exec 3<>/dev/tcp/127.0.0.1/{port}) 2>/dev/null && echo up || "
            f"grep -qi ':{hexport} ' /proc/net/tcp /proc/net/tcp6 2>/dev/null "
            f"&& echo up || true", timeout=15)
        return "up" in r.out

    def _target_host(self, service: dict) -> str:
        """Where the process actually runs: slurm services land on a
        compute node; the tunnel hops through the login connection."""
        adapter = self._adapter(service["site"])
        handle = service["handle"]
        if handle.startswith("slurm:") and hasattr(adapter, "run_cmd"):
            jid = handle.split(":", 1)[1]
            r = adapter.run_cmd(
                f"squeue -h -j {shlex.quote(jid)} -o %N 2>/dev/null; true",
                timeout=15)
            node = r.out.strip().split()[0] if r.out.strip() else ""
            if node and node not in ("(null)", "n/a"):
                return node
        return "127.0.0.1"

    def _establish_endpoints(self, service_id: str) -> list[dict]:
        s = self._get(service_id)
        adapter = self._adapter(s["site"])
        if not hasattr(adapter, "ssh_transport_opts"):
            # local site: the endpoint already is local
            return [{"port": p, "url": f"http://127.0.0.1:{p}"}
                    for p in s["ports"]]
        target = self._target_host(s)
        endpoints, tunnels = [], []
        for p in s["ports"]:
            lp = _free_local_port()
            proc = subprocess.Popen(
                ["ssh", *adapter.ssh_transport_opts(), "-f", "-N",
                 "-o", "ExitOnForwardFailure=yes",
                 # multi-hop links drop; a dead tunnel must EXIT (so the
                 # liveness check sees it) rather than hang half-open
                 "-o", "ServerAliveInterval=15",
                 "-o", "ServerAliveCountMax=3",
                 "-L", f"127.0.0.1:{lp}:{target}:{p}",
                 adapter.destination()],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                _, err = proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                err = b""
            if proc.returncode not in (0, None):
                raise WeftError(
                    "site.unreachable",
                    f"could not open tunnel to {target}:{p}",
                    stage="infra", retryable=True,
                    hints={"stderr": err.decode()[-300:]})
            endpoints.append({"port": p, "local_port": lp,
                              "url": f"http://127.0.0.1:{lp}"})
            tunnels.append({"local_port": lp, "remote": f"{target}:{p}"})
        self._tunnels[service_id] = tunnels
        return endpoints

    def status(self, service_id: str) -> dict:
        s = self._get(service_id)
        out = {"service_id": service_id, "site": s["site"],
               "state": s["state"], "ports": s["ports"]}
        if s["state"] == "ready":
            adapter = self._adapter(s["site"])
            # re-hook the poller after a controller restart
            if not self.runner.poller_for(s["site"]).watching(service_id):
                from .poller import Watch
                from .task import Task
                self.runner.poller_for(s["site"]).register(Watch(
                    job_id=service_id, handle=s["handle"],
                    jobdir_rel=s["jobdir"], task=Task.from_dict(s["task"]),
                    started_at=time.time(), scheduler=False, lease="service"))
            if not self._tunnels.get(service_id):
                out["endpoints"] = self._establish_endpoints(service_id)
            elif not self._tunnels_alive(service_id):
                # a hop dropped and the keepalive killed the tunnel:
                # SELF-HEAL on status, loudly — a stale endpoint that
                # hangs is worse than a moment of reconnection
                self.store.emit("service.tunnel_lost", service=service_id,
                                site=s["site"])
                self._close_tunnels(service_id)
                out["endpoints"] = self._establish_endpoints(service_id)
                self.store.emit("service.tunnel_restored",
                                service=service_id, site=s["site"])
                out["tunnel_note"] = "tunnel dropped and was re-established"
            else:
                s2 = self._tunnels[service_id]
                out["endpoints"] = [
                    {"port": p, "local_port": t["local_port"],
                     "url": f"http://127.0.0.1:{t['local_port']}"}
                    for p, t in zip(s["ports"], s2)]
            out["tunnels_alive"] = True
        else:
            out["log_tail"] = self.runner.tail_log(
                self._adapter(s["site"]), s["jobdir"], 30)
        return out

    def stop(self, service_id: str, collect: bool = False) -> dict:
        s = self._get(service_id)
        adapter = self._adapter(s["site"])
        self.runner.poller_for(s["site"]).notify_cancel(service_id)
        adapter.cancel(s["handle"], s["jobdir"])
        self._close_tunnels(service_id)
        self.store.update_service(service_id, state="stopped")
        out = {"service_id": service_id, "state": "stopped"}
        if collect:
            from .task import Task
            t = Task.from_dict(s["task"])
            if t.outputs:
                entries, total = self.dataman.collect_outputs(
                    adapter, s["jobdir"], t)
                out["outputs"] = entries
                out["output_bytes"] = total
                self.store.emit("service.collected", service=service_id,
                                outputs=len(entries), output_bytes=total)
        self.store.emit("service.stopped", service=service_id)
        return out

    def _tunnels_alive(self, service_id: str) -> bool:
        """Is a local listener still up for every tunnel? (The -f tunnels
        daemonize; ServerAlive kills them on a dead link, which frees the
        local port — exactly what this detects.)"""
        import socket
        for t in self._tunnels.get(service_id, []):
            try:
                with socket.create_connection(
                        ("127.0.0.1", t["local_port"]), timeout=2):
                    pass
            except OSError:
                return False
        return True

    def _close_tunnels(self, service_id: str) -> None:
        # -f tunnels daemonize; close by targeting their forwarded port
        for t in self._tunnels.pop(service_id, []):
            subprocess.run(
                ["pkill", "-f", f"127.0.0.1:{t['local_port']}:"],
                capture_output=True)
