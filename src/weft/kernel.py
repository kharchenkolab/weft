"""Persistent interactive kernels (design-next §3): a tracked, detached
interpreter fed code blocks through files — Jupyter-like statefulness
without sockets, daemons, or protocol tunnels.

A kernel is a special detached job: it survives disconnects, is watched by
the site poller (death → `kernel.died` naming the killing block), is
bounded by walltime, and leaves an ordered transcript (code + outputs)
that is itself the provenance trail of the exploration. Doctrine: kernels
are for exploration; the citable record is the assembled script re-run as
a normal task.

Languages are a registry — adding one = a driver file + an entry here.
"""

from __future__ import annotations

import shlex
import time
import uuid
from pathlib import Path

from .errors import WeftError
from .realize import env_dir_rel

_DRIVER_DIR = Path(__file__).resolve().parent / "kernels"

# lang -> (driver filename, interpreter argv prefix)
LANGUAGES: dict[str, tuple[str, str]] = {
    "python": ("driver.py", "python3 -u"),
    "r": ("driver.R", "Rscript"),
    "julia": ("driver.jl", "julia"),
}


class KernelManager:
    def __init__(self, store, adapters, runner):
        self.store = store
        self.adapters = adapters
        self.runner = runner

    def _adapter(self, site: str):
        if site not in self.adapters:
            raise WeftError("task.invalid", f"unknown site: {site}",
                            stage="infra",
                            hints={"registered": sorted(self.adapters)})
        return self.adapters[site]

    def _get(self, kernel_id: str) -> dict:
        k = self.store.get_kernel(kernel_id)
        if not k:
            raise WeftError("task.invalid", f"unknown kernel: {kernel_id}",
                            stage="infra")
        return k

    # -- lifecycle ------------------------------------------------------------

    def start(self, site: str, lang: str = "python",
              env_id: str | None = None, walltime: str = "08:00:00",
              resources: dict | None = None, label: str = "") -> dict:
        """`resources` places the INTERACTIVE session like a job: on slurm,
        {"gpus": 1, "partition": "gpu"} holds a GPU-node allocation and
        the kernel lives inside it — analysis on the node, not the login
        host. The file-block protocol needs no ports: the shared
        filesystem IS the channel."""
        if lang not in LANGUAGES:
            raise WeftError(
                "task.invalid", f"no kernel driver for lang {lang!r}",
                stage="infra", hints={"registered": sorted(LANGUAGES)})
        if len(label) > 200:
            raise WeftError(
                "task.invalid",
                f"label is {len(label)} chars (max 200) — labels are "
                "display handles, not documents", stage="infra")
        adapter = self._adapter(site)
        driver_file, interp = LANGUAGES[lang]

        activate = "true"
        if env_id:
            rel = env_dir_rel(env_id)
            if not adapter.file_exists(f"{rel}/.weft-ready"):
                raise WeftError(
                    "env.not_realized",
                    f"env {env_id} is not realized on {site}",
                    stage="realize",
                    hints={"suggestion": "run any task with this env on the "
                                         "site first (even `true`) — kernels "
                                         "attach to realized envs"})
            activate = f". {shlex.quote(adapter.path(rel))}/activate.sh"

        kernel_id = "krn_" + uuid.uuid4().hex[:10]
        jobdir_rel = f"kernels/{kernel_id}"
        driver_src = (_DRIVER_DIR / driver_file).read_bytes()
        adapter.write_file(f"{jobdir_rel}/{driver_file}", driver_src)
        adapter.write_file(f"{jobdir_rel}/activate.sh", (activate + "\n").encode())
        adapter.write_file(
            f"{jobdir_rel}/cmd.sh",
            f"mkdir -p blocks\nexec {interp} {driver_file}\n".encode())
        if env_id and self.runner.ns_wrap_needed(env_id, site):
            # the kernel's whole life runs inside one mount namespace;
            # its env mount dies with the driver
            adapter.write_file(f"{jobdir_rel}/ns", b"1\n")
        res = {"cpus": 1, "walltime": walltime, **(resources or {})}
        # the same fence tasks get: an ask no partition can hold would sit
        # PENDING forever (PartitionTimeLimit) — an interactive session
        # that silently never starts is the worst kind of wait
        caps = (self.store.get_site(site) or {}).get("capabilities") or {}
        parts = (caps.get("scheduler") or {}).get("partitions") or None
        if parts:
            if res.get("partition"):
                parts = [p for p in parts
                         if p["name"] == res["partition"]] or parts
            from .capability import satisfies_resources
            ok, hints = satisfies_resources(caps, res, partitions=parts)
            if not ok:
                raise WeftError(
                    "site.capability_violation",
                    f"kernel ask exceeds what {site} offers "
                    "(it would queue forever)",
                    stage="submit",
                    hints={**hints,
                           "suggestion": "shrink walltime/resources to a "
                                         "fitting partition — interactive "
                                         "kernels usually want SHORT "
                                         "walltimes and a re-start, not a "
                                         "day-long hold"})
        handle = adapter.submit(jobdir_rel, {"resources": res})
        self.store.put_kernel(kernel_id, site, lang, env_id, jobdir_rel,
                              handle, label=label)
        self.store.emit("kernel.started", kernel=kernel_id, site=site,
                        lang=lang, env_id=env_id, resources=res,
                        **({"label": label} if label else {}))
        from .poller import Watch
        from .task import Task
        self.runner.poller_for(site).register(Watch(
            job_id=kernel_id, handle=handle, jobdir_rel=jobdir_rel,
            task=Task.from_dict({"command": f"[kernel {lang}]",
                                 "resources": {"walltime": ""}}),
            started_at=time.time(), scheduler=False, lease="kernel"))
        return {"kernel_id": kernel_id, "site": site, "lang": lang,
                "env_id": env_id,
                "note": "exploration only — assemble successful blocks into "
                        "a task for the citable record"}

    # -- block execution ---------------------------------------------------------

    def exec(self, kernel_id: str, code: str, wait: bool = True,
             timeout: float = 120.0) -> dict:
        k = self._get(kernel_id)
        self._assert_alive(k)
        adapter = self._adapter(k["site"])
        n = k["blocks_run"]
        adapter.write_file(f"{k['jobdir']}/blocks/{n:04d}.code", code.encode())
        self.store.update_kernel(kernel_id, blocks_run=n + 1)
        if not wait:
            return {"kernel_id": kernel_id, "block": n, "state": "submitted"}
        return self.poll(kernel_id, n, timeout=timeout)

    def poll(self, kernel_id: str, block: int, timeout: float = 0.0) -> dict:
        k = self._get(kernel_id)
        adapter = self._adapter(k["site"])
        base = f"{k['jobdir']}/blocks/{block:04d}"
        deadline = time.time() + timeout
        last_alive_check = 0.0
        while True:
            # a death must surface immediately, not after the full timeout
            if time.time() - last_alive_check > 2.0:
                last_alive_check = time.time()
                self._assert_alive(self._get(kernel_id))
            if adapter.file_exists(f"{base}.rc"):
                rc = int(adapter.read_file(f"{base}.rc").decode().strip() or 1)
                out = adapter.read_file(f"{base}.out", 65536).decode("utf-8", "replace")
                err = adapter.read_file(f"{base}.err", 16384).decode("utf-8", "replace")
                arts = adapter.run_cmd(
                    f"ls {shlex.quote(adapter.path(base + '.artifacts'))} 2>/dev/null"
                ).out.split()
                if rc != 0:
                    self.store.emit("kernel.block_failed", kernel=kernel_id,
                                    block=block, rc=rc, err_tail=err[-500:])
                return {"kernel_id": kernel_id, "block": block, "rc": rc,
                        "out": out, "err": err, "artifacts": arts,
                        "state": "done"}
            if time.time() >= deadline:
                # not an error: the block may legitimately be long-running
                self._assert_alive(self._get(kernel_id))
                return {"kernel_id": kernel_id, "block": block,
                        "state": "running",
                        "note": "still executing — kernel_poll to keep "
                                "waiting, kernel_status for a look, "
                                "kernel_interrupt to stop the block"}
            time.sleep(min(0.3, max(deadline - time.time(), 0.05)))

    def _assert_alive(self, k: dict) -> None:
        if k["state"] != "running":
            raise WeftError(
                "sched.node_failure",
                f"kernel {k['kernel_id']} is {k['state']}",
                stage="running",
                hints={"suggestion": "kernel_restart(kernel_id, "
                                     "replay='successful') starts a NEW "
                                     "kernel (use the returned kernel_id) "
                                     "with state rebuilt from the transcript",
                       "transcript": "kernel_transcript shows what ran"})

    # -- introspection / control ---------------------------------------------------

    def status(self, kernel_id: str) -> dict:
        k = self._get(kernel_id)
        adapter = self._adapter(k["site"])
        current = None
        try:
            if adapter.file_exists(f"{k['jobdir']}/current_block"):
                current = int(adapter.read_file(
                    f"{k['jobdir']}/current_block").decode().strip())
        except (WeftError, ValueError):
            pass
        return {"kernel_id": kernel_id, "site": k["site"], "lang": k["lang"],
                "label": k.get("label") or None,
                "env_id": k["env_id"], "state": k["state"],
                "blocks_run": k["blocks_run"], "current_block": current,
                "idle_s": round(time.time() - k["last_used"], 1)}

    def transcript(self, kernel_id: str, last: int = 20) -> list[dict]:
        k = self._get(kernel_id)
        adapter = self._adapter(k["site"])
        out = []
        for n in range(max(0, k["blocks_run"] - last), k["blocks_run"]):
            base = f"{k['jobdir']}/blocks/{n:04d}"
            entry = {"block": n}
            try:
                entry["code"] = adapter.read_file(f"{base}.code", 4096).decode()
                if adapter.file_exists(f"{base}.rc"):
                    entry["rc"] = int(adapter.read_file(f"{base}.rc"
                                                        ).decode().strip() or 1)
                    entry["out_tail"] = adapter.read_file(
                        f"{base}.out", 2048).decode("utf-8", "replace")
                else:
                    entry["rc"] = None  # still running / never ran
            except WeftError:
                entry["error"] = "unreadable"
            out.append(entry)
        return out

    def interrupt(self, kernel_id: str) -> dict:
        k = self._get(kernel_id)
        adapter = self._adapter(k["site"])
        pid = adapter.read_file(f"{k['jobdir']}/pid.real").decode().strip()
        # `kill -s INT -- -pgid`: the only group-kill form dash accepts
        adapter.run_cmd(f"kill -s INT -- -{shlex.quote(pid)} 2>/dev/null; true")
        self.store.emit("kernel.interrupted", kernel=kernel_id)
        return {"kernel_id": kernel_id, "note": "SIGINT sent; the running "
                "block should finish with rc=130"}

    def stop(self, kernel_id: str) -> dict:
        k = self._get(kernel_id)
        adapter = self._adapter(k["site"])
        adapter.write_file(f"{k['jobdir']}/kernel.stop", b"1\n")
        self.runner.poller_for(k["site"]).notify_cancel(kernel_id)
        time.sleep(0.5)
        adapter.cancel(k["handle"], k["jobdir"])
        self.store.update_kernel(kernel_id, state="stopped")
        self.store.emit("kernel.stopped", kernel=kernel_id)
        return {"kernel_id": kernel_id, "state": "stopped"}

    def promote(self, kernel_id: str, blocks: list[int],
                dataman=None) -> dict:
        """Elevate exploratory kernel results into the record — honestly.

        Captures the FULL ordered transcript through the last promoted
        block (the state that produced block N is a function of blocks
        0..N, all of which we hold) plus the promoted blocks' artifacts,
        into a manifest with reproducibility="transcript": replayable (the
        same mechanism kernel_restart uses), one rung below content-pinned
        tasks, above post_install's "weak". Default doctrine is unchanged —
        promotion is explicit and labeled."""
        import uuid as _uuid
        k = self._get(kernel_id)
        if not blocks:
            raise WeftError("task.invalid", "blocks=[...] required",
                            stage="collecting")
        adapter = self._adapter(k["site"])
        last = max(blocks)
        transcript = self.transcript(kernel_id, last=10**6)
        by_block = {e["block"]: e for e in transcript}
        for b in blocks:
            e = by_block.get(b)
            if e is None or e.get("rc") is None:
                raise WeftError("task.invalid",
                                f"block {b} has not finished",
                                stage="collecting")
            if e["rc"] != 0:
                raise WeftError(
                    "task.invalid",
                    f"block {b} failed (rc={e['rc']}) — only successful "
                    "blocks can be promoted", stage="collecting",
                    hints={"transcript": "kernel_transcript shows rc per block"})

        from .task import Task
        pseudo = Task.from_dict({
            "command": f"[kernel promotion of blocks {blocks}]",
            "env": k["env_id"],
            "outputs": [f"blocks/{b:04d}.artifacts/" for b in blocks],
            "site": k["site"]})
        entries, total = dataman.collect_outputs(adapter, k["jobdir"], pseudo)
        from .ids import task_id
        chain = [{"block": e["block"], "code": e.get("code", ""),
                  "rc": e.get("rc")} for e in transcript
                 if e["block"] <= last]
        job_id = "jb_" + _uuid.uuid4().hex[:12]
        from .grade import grade_env, grade_manifest
        env_row = self.store.get_env(k["env_id"]) if k["env_id"] else None
        g = grade_manifest(
            grade_env(env_row["canonical"]) if env_row else None,
            transcript=True)
        manifest = {
            "schema": "manifest:v1",
            "reproducibility": g["grade"],
            "reproducibility_meaning": g["meaning"],
            "reproducibility_components": g["components"],
            "job_id": job_id, "kernel_id": kernel_id,
            "task_hash": task_id({"kernel_transcript": chain,
                                  "env": k["env_id"]}),
            "env_id": k["env_id"], "site": k["site"],
            "exit_code": 0, "wall_s": None, "max_rss_gb": None,
            "transcript": chain,
            "outputs": entries, "output_bytes": total,
            "logs": {"tail": "", "site_path": adapter.path(k["jobdir"])},
        }
        self.store.put_job(job_id, manifest["task_hash"], pseudo.to_dict(),
                           k["site"], "DONE")
        self.store.update_job(job_id, manifest=manifest)
        self.store.emit("kernel.promoted", kernel=kernel_id, job_id=job_id,
                        blocks=blocks)
        self.store.audit_log(None, "kernel.promote", site=k["site"],
                             command=f"{kernel_id} blocks={blocks}")
        return manifest

    def restart(self, kernel_id: str, replay: str = "successful") -> dict:
        """After a death (or deliberately): starts a NEW kernel (fresh
        kernel_id — returned; the old one stays dead/stopped for its
        transcript), same env/site, optionally replaying the old
        transcript's successful blocks to rebuild interpreter state."""
        k = self._get(kernel_id)
        codes = []
        if replay == "successful":
            for entry in self.transcript(kernel_id, last=10**6):
                if entry.get("rc") == 0 and "code" in entry:
                    codes.append(entry["code"])
        if k["state"] == "running":
            self.stop(kernel_id)
        # the label names the WORK, which continues in the successor
        fresh = self.start(k["site"], k["lang"], k["env_id"],
                           label=k.get("label") or "")
        replayed = 0
        for code in codes:
            r = self.exec(fresh["kernel_id"], code, wait=True, timeout=300)
            if r.get("rc") == 0:
                replayed += 1
        self.store.emit("kernel.restarted", kernel=fresh["kernel_id"],
                        previous=kernel_id, replayed=replayed)
        return {**fresh, "previous": kernel_id, "replayed_blocks": replayed}
