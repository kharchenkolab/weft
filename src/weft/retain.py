"""Plain-file retention of run outputs (misc/retention.md).

Retention keeps chosen files from a run's sandbox findable and
browsable PAST the sandbox's life — as ordinary files, no CAS, no
per-file bookkeeping. Placement is discovered per file, never promised:
reflink -> hardlink -> copy locally; in-place under a site's declared
retain.dir; tar-pipe transfer home otherwise. Run-level provenance
rides in one sidecar. The producing run's terminal INVENTORY is
knowledge and lives elsewhere (run_inventories) — it survives retain,
discard and forget alike.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .errors import WeftError

TERMINAL_JOB = ("DONE", "FAILED", "CANCELLED")
TERMINAL_KERNEL = ("stopped", "died")


class RetainManager:
    def __init__(self, store, adapters, workspace: Path):
        self.store = store
        self.adapters = adapters
        self.workspace = Path(workspace)

    # -- target facts -------------------------------------------------------

    def _target_row(self, target: str) -> tuple[str, dict, str]:
        """-> (kind, row, jobdir_rel)"""
        job = self.store.get_job(target)
        if job:
            return "job", job, f"jobs/{target}"
        k = self.store.get_kernel(target)
        if k:
            return "kernel", k, k["jobdir"]
        raise WeftError("task.invalid", f"unknown run: {target}",
                        stage="infra")

    def _guard_finished(self, kind: str, row: dict, selected: list[dict],
                        adapter) -> None:
        """Retention operates on FINISHED things (retention.md decision
        2): terminal runs, or — for a live kernel — files under
        completed blocks' artifact dirs (immutable once .rc landed)."""
        state = row["state"]
        if kind == "job" and state in TERMINAL_JOB:
            return
        if kind == "kernel" and state in TERMINAL_KERNEL:
            return
        if kind == "kernel":     # live kernel: completed-block dirs only
            import re
            bad, checked = [], {}
            for e in selected:
                m = re.match(r"blocks/(\d{4})\.artifacts/", e["path"])
                if not m:
                    bad.append(e["path"])
                    continue
                n = m.group(1)
                if n not in checked:
                    checked[n] = adapter.file_exists(
                        f"{row['jobdir']}/blocks/{n}.rc")
                if not checked[n]:
                    bad.append(e["path"])
            if bad:
                raise WeftError(
                    "task.invalid",
                    "kernel is running — only completed blocks' artifact "
                    "dirs are retainable mid-life",
                    stage="infra",
                    hints={"not_retainable": bad[:10],
                           "note": "files must be written under "
                                   "$WEFT_BLOCK_DIR to be retainable "
                                   "before the kernel stops",
                           "suggestion": "narrow include= to "
                                         "blocks/*.artifacts/**, or "
                                         "kernel_stop first"})
            return
        raise WeftError(
            "task.invalid",
            f"run {target_desc(kind, row)} is {state!r} — retention "
            "operates on finished things", stage="infra",
            hints={"suggestion": "wait for a terminal state (or stop the "
                                 "kernel); live kernels may retain "
                                 "completed blocks' artifacts"})

    def _scan(self, adapter, jobdir_rel: str) -> list[dict]:
        r = adapter.shim(["list-tree", "--root", adapter.path(jobdir_rel),
                          "--max", "100000"], timeout=600)
        if r.rc != 0:
            raise WeftError("data.missing",
                            f"cannot scan run sandbox: {r.err[:200]}",
                            stage="staging", retryable=True)
        out = []
        for line in r.out.splitlines():
            p = line.split("\t")
            if len(p) >= 3:
                out.append({"path": p[0], "bytes": int(p[1]),
                            "mtime": int(p[2])})
        return out

    @staticmethod
    def _select(entries: list[dict], include, exclude) -> list[dict]:
        inc = include or ["**"]
        exc = exclude or []

        def hit(path, pats):
            return any(fnmatch.fnmatch(path, p)
                       or fnmatch.fnmatch(path, p.rstrip("/") + "/*")
                       for p in pats)
        return [e for e in entries
                if hit(e["path"], inc) and not hit(e["path"], exc)]

    # -- the verb -----------------------------------------------------------

    def retain(self, target: str, include=None, exclude=None,
               dest: str | None = None, max_gb: float | None = None,
               label: str = "", background: bool = True,
               headroom_gb: float = 20.0) -> dict:
        if label and len(label) > 200:
            raise WeftError("task.invalid", "label max 200 chars",
                            stage="infra")
        kind, row, jobdir_rel = self._target_row(target)
        site = row["site"]
        adapter = self.adapters.get(site)
        if adapter is None:
            raise WeftError("site.unreachable",
                            f"site {site!r} not registered", stage="infra")
        selected = self._select(self._scan(adapter, jobdir_rel),
                                include, exclude)
        self._guard_finished(kind, row, selected, adapter)
        if not selected:
            raise WeftError("data.missing",
                            "selection matched no files", stage="staging",
                            hints={"include": include, "exclude": exclude})
        total = sum(e["bytes"] for e in selected)
        if max_gb is not None and total > max_gb * 1e9:
            raise WeftError(
                "task.invalid",
                f"selection is {total/1e9:.2f} GB, over the "
                f"{max_gb} GB cap", stage="staging",
                hints={"files": len(selected),
                       "suggestion": "narrow include/exclude or raise "
                                     "max_gb"})

        site_cfg = (self.store.get_site(site) or {}).get("config") or {}
        retain_dir = ((site_cfg.get("retain") or {}).get("dir")
                      if not dest else None)
        local_like = adapter.__class__.__name__ == "LocalAdapter" or \
            getattr(adapter, "transport", "ssh") == "local"
        if retain_dir:
            location = f"{retain_dir.rstrip('/')}/runs/{target}"
            in_place = True
        else:
            location = str(Path(dest) if dest
                           else self.workspace / "runs" / target)
            in_place = False
            free = shutil.disk_usage(
                Path(location).parent if Path(location).parent.exists()
                else self.workspace).free
            if free - total < headroom_gb * 1e9:
                raise WeftError(
                    "task.invalid",
                    f"retaining {total/1e9:.2f} GB would leave "
                    f"{(free-total)/1e9:.2f} GB free (< {headroom_gb} GB "
                    "headroom)", stage="staging",
                    hints={"free_gb": round(free / 1e9, 2),
                           "suggestion": "narrow the selection or lower "
                                         "policy headroom deliberately"})

        self.store.put_retained(target, site, label or None, location,
                                in_place, len(selected), total,
                                state="queued",
                                selection={"include": include,
                                           "exclude": exclude,
                                           "dest": dest})

        def work():
            try:
                self.store.update_retained(target, state="inflight")
                method = self._place(adapter, jobdir_rel, selected,
                                     location, in_place, local_like)
                self._sidecar(kind, row, target, site, label, selected,
                              location, in_place, method, adapter)
                self.store.update_retained(target, state="done",
                                           method=method)
                self.store.emit("retain.done", target=target, site=site,
                                files=len(selected), bytes=total,
                                method=method, location=location,
                                **({"label": label} if label else {}))
            except WeftError as e:
                self.store.update_retained(target, state="failed",
                                           error=json.dumps(e.to_dict()))
                self.store.emit("retain.failed", target=target,
                                **e.to_dict())
            except Exception as e:  # noqa: BLE001 — surfaced, not raised
                self.store.update_retained(
                    target, state="failed",
                    error=json.dumps({"error": "internal.error",
                                      "detail": repr(e)}))
                self.store.emit("retain.failed", target=target,
                                error="internal.error", detail=repr(e))

        if background:
            threading.Thread(target=work, daemon=True).start()
            state = "queued"
        else:
            work()
            state = self.store.get_retained(target)["state"]
        return {"target": target, "files": len(selected), "bytes": total,
                "location": {"site": site if in_place else "@workspace",
                             "path": location},
                "in_place": in_place, "state": state,
                **({"label": label} if label else {})}

    # -- placement ----------------------------------------------------------

    def _place(self, adapter, jobdir_rel, selected, location,
               in_place, local_like) -> str:
        src_root = adapter.path(jobdir_rel)
        if in_place or local_like:
            # same machine as the files (site-side or local): the
            # discovered chain — reflink -> hardlink -> copy
            script = [f"set -e; mkdir -p {shlex.quote(location)}"]
            for e in selected:
                s = shlex.quote(f"{src_root}/{e['path']}")
                d = shlex.quote(f"{location}/{e['path']}")
                dd = shlex.quote(os.path.dirname(f"{location}/{e['path']}"))
                script.append(
                    f"mkdir -p {dd}; "
                    f"cp -c {s} {d} 2>/dev/null || "
                    f"cp --reflink=always {s} {d} 2>/dev/null || "
                    f"ln -f {s} {d} 2>/dev/null || cp -p {s} {d}")
            script.append(f"echo placed")
            r = adapter.run_cmd("\n".join(script), timeout=3600)
            if r.rc != 0 or "placed" not in r.out:
                raise WeftError("data.transfer_failed",
                                f"placement failed: {(r.err or r.out)[-300:]}",
                                stage="staging", retryable=True)
            return "reflink|link|copy"
        # remote -> workspace: tar pipe over the adapter's transport
        # (rsync-free: works on bare sites; one-shot pulls don't need
        # delta transfer)
        Path(location).mkdir(parents=True, exist_ok=True)
        file_list = "\n".join(e["path"] for e in selected) + "\n"
        remote = (f"cd {shlex.quote(src_root)} && tar cf - -T -")
        ssh_cmd = ["ssh", *adapter.ssh_transport_opts(),
                   adapter.destination(), remote]
        tar_in = subprocess.Popen(ssh_cmd, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE)
        tar_out = subprocess.Popen(["tar", "xf", "-"], cwd=location,
                                   stdin=tar_in.stdout)
        tar_in.stdin.write(file_list.encode())
        tar_in.stdin.close()
        tar_out.wait(timeout=6 * 3600)
        tar_in.wait(timeout=60)
        if tar_out.returncode != 0 or tar_in.returncode != 0:
            raise WeftError("data.transfer_failed",
                            "tar-pipe transfer failed",
                            stage="staging", retryable=True)
        missing = [e["path"] for e in selected
                   if not (Path(location) / e["path"]).exists()]
        if missing:
            raise WeftError("data.transfer_failed",
                            f"{len(missing)} file(s) missing after "
                            "transfer", stage="staging", retryable=True,
                            hints={"missing": missing[:5]})
        return "transfer"

    # -- GC: the other half ---------------------------------------------------

    def discard(self, target: str) -> dict:
        """Delete a finished run's SANDBOX now. Retained files are
        unaffected (they live elsewhere, or the surviving hardlink keeps
        the inode); the terminal inventory — knowledge — survives."""
        kind, row, jobdir_rel = self._target_row(target)
        state = row["state"]
        if (kind == "job" and state not in TERMINAL_JOB) or \
                (kind == "kernel" and state not in TERMINAL_KERNEL):
            raise WeftError("task.invalid",
                            f"run is {state!r} — discard finished runs "
                            "only", stage="infra")
        adapter = self.adapters.get(row["site"])
        if adapter is None:
            raise WeftError("site.unreachable",
                            f"site {row['site']!r} not registered",
                            stage="infra", retryable=True)
        adapter.run_cmd(
            f"rm -rf {shlex.quote(adapter.path(jobdir_rel))}",
            timeout=1800)
        self.store.emit("run.discarded", target=target, site=row["site"])
        return {"target": target, "state": "discarded",
                "note": "sandbox deleted; retained files and the "
                        "terminal inventory survive"}

    def forget(self, target: str | None = None,
               label: str | None = None) -> dict:
        """Explicit reclamation of the RETAINED tier: delete the retained
        tree + sidecar wherever the bytes live, drop the index row ON
        CONFIRMED deletion (site unreachable -> row parked
        forget_pending, call again later). Idempotent. The terminal
        inventory — knowledge — always survives (see retention.md for
        why there is deliberately no keep_inventory flag)."""
        if bool(target) == bool(label):
            raise WeftError("task.invalid",
                            "exactly one of target= or label=",
                            stage="infra")
        rows = ([self.store.get_retained(target)] if target
                else self.store.retained_where(label=label))
        rows = [r for r in rows if r]
        forgotten, pending = [], []
        for row in rows:
            if row["state"] in ("queued", "inflight"):
                # deleting a destination WHILE a transfer writes into it
                # would race — refuse until the retain settles
                pending.append({"target": row["target"],
                                "why": "retain.in_flight",
                                "retryable": True})
                continue
            try:
                if row["in_place"]:
                    adapter = self.adapters.get(row["site"])
                    if adapter is None:
                        raise WeftError("site.unreachable",
                                        f"site {row['site']!r} not "
                                        "registered", stage="infra",
                                        retryable=True)
                    r = adapter.run_cmd(
                        f"rm -rf {shlex.quote(row['location'])}",
                        timeout=1800)
                    if r.rc != 0:
                        raise WeftError("site.unreachable",
                                        f"delete failed: {r.err[:200]}",
                                        stage="infra", retryable=True)
                else:
                    shutil.rmtree(row["location"], ignore_errors=True)
                self.store.delete_retained(row["target"])
                self.store.emit("retain.forgotten", target=row["target"],
                                bytes=row["bytes"])
                forgotten.append({"target": row["target"],
                                  "bytes": row["bytes"],
                                  "location": row["location"]})
            except WeftError as e:
                self.store.update_retained(row["target"],
                                           state="forget_pending",
                                           error=json.dumps(e.to_dict()))
                pending.append({"target": row["target"],
                                "why": e.code, "retryable": e.retryable})
        out = {"forgotten": forgotten,
               "bytes_reclaimed": sum(f["bytes"] or 0 for f in forgotten)}
        if pending:
            out["forget_pending"] = pending
            out["note"] = ("pending rows keep their index entries; call "
                           "run_forget again when the site is reachable")
        if not rows:
            out["note"] = "nothing retained under that target/label " \
                          "(already forgotten?)"
        return out

    # -- the receipt beside the files ----------------------------------------

    def _sidecar(self, kind, row, target, site, label, selected,
                 location, in_place, method, adapter) -> None:
        manifest = (row.get("manifest") or {}) if kind == "job" else {}
        doc = {
            "schema": "weft-run:v1",
            "target": target, "kind": kind, "site": site,
            "label": label or None,
            "retained_at": time.time(),
            "method": method,
            "node": manifest.get("node"),
            "env_id": (row.get("task") or {}).get("env")
            if kind == "job" else row.get("env_id"),
            "command": (row.get("task") or {}).get("command")
            if kind == "job" else f"[kernel {target} transcript in "
                                  f"workspace store]",
            "files": [{k: e[k] for k in ("path", "bytes", "mtime")}
                      for e in selected],
        }
        body = json.dumps(doc, indent=1).encode()
        if in_place:
            q = shlex.quote(body.decode())
            adapter.run_cmd(f"printf %s {q} > "
                            f"{shlex.quote(location + '/.weft-run.json')}",
                            timeout=120)
        else:
            (Path(location) / ".weft-run.json").write_bytes(body)


def target_desc(kind: str, row: dict) -> str:
    return row.get("job_id") or row.get("kernel_id") or kind
