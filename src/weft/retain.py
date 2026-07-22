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


def storage_facts(config: dict) -> dict:
    """The site's declared storage facts (retention2.md): `durable`
    answers "is there durable storage on this node, and where?" —
    absent/False = no, True = the root itself, "<abs path>" = there.
    NEVER guessed: durability is a user assertion. The path heuristic
    exists only to phrase a courtesy hint at registration."""
    config = config or {}
    d = config.get("durable")
    if d is None:
        legacy = (config.get("retain") or {}).get("dir")
        if legacy:
            return {"durable": legacy,
                    "source": "retain.dir (deprecated — use durable=)"}
    if d is True:
        return {"durable": True, "source": "declared"}
    if isinstance(d, str):
        if not d.startswith("/"):
            raise WeftError(
                "task.invalid",
                f"durable must be True or an absolute path (got {d!r})",
                stage="infra")
        return {"durable": d, "source": "declared"}
    if d not in (None, False):
        raise WeftError(
            "task.invalid",
            f"durable must be True, False/absent, or an absolute path "
            f"(got {d!r})", stage="infra")
    out = {"durable": None, "source": "not declared"}
    root = str(config.get("root") or "")
    if root.startswith(("/home/", "/users/", "/user/")) or "/home/" in root:
        out["hint"] = (f"root {root} looks like a home filesystem — "
                       f"add durable=true if weft may keep files there")
    elif root.startswith(("/scratch", "/tmp", "/var/tmp")) \
            or "/scratch" in root:
        out["hint"] = (f"root {root} looks like scratch — retains will "
                       f"need dest= (e.g. '@workspace') or a "
                       f"durable=<path> declaration")
    return out

# every file weft itself writes into a run sandbox — the CONTRACT that
# lets a host split "what the run produced" from weft's plumbing without
# guessing from names (weft-ui ask). Kept next to the writers: runner
# (cmd.sh/activate.sh/inputs.tsv/ingest.tsv/ns), shim job-start
# (runner.sh/log/pid*/exit_code/wall_s/rusage), kernel drivers
# (driver.*, blocks/NNNN.{code,out,err,rc} — but blocks/*.artifacts/**
# is $WEFT_BLOCK_DIR: USER files).
_SCAFFOLD_EXACT = {
    "cmd.sh", "activate.sh", "inputs.tsv", "ingest.tsv", "ns",
    "runner.sh", "log", "log.err", "pid", "pid.real", "exit_code",
    "wall_s", "rusage", "driver.py", "driver.R", "driver.jl",
}


def is_scaffold(path: str) -> bool:
    if path in _SCAFFOLD_EXACT:
        return True
    if path.startswith("blocks/"):
        # top-level protocol files only; anything nested (NNNN.artifacts/)
        # is the block's saved output — the user's
        return "/" not in path[len("blocks/"):]
    return False


def mark_scaffold(entries: list[dict]) -> list[dict]:
    """Flag weft-written files in a scan/inventory entry list, in place."""
    for e in entries:
        if is_scaffold(e.get("path", "")):
            e["scaffold"] = True
    return entries


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
        return mark_scaffold(out)

    @staticmethod
    def _select(entries: list[dict], include, exclude) -> list[dict]:
        # with no explicit include, "retain everything" means everything
        # the RUN produced — weft's own plumbing (scaffold) stays out.
        # An explicit include is sovereign: it selects whatever it names.
        if include is None:
            entries = [e for e in entries if not e.get("scaffold")]
        inc = include or ["**"]
        exc = exclude or []

        def hit(path, pats):
            return any(fnmatch.fnmatch(path, p)
                       or fnmatch.fnmatch(path, p.rstrip("/") + "/*")
                       for p in pats)
        return [e for e in entries
                if hit(e["path"], inc) and not hit(e["path"], exc)]

    # -- the verb -----------------------------------------------------------

    def _placement(self, target: str, site: str, jobdir_rel: str,
                   dest: str | None, label: str, layout: str,
                   adapter) -> dict:
        """retention2.md PLACE: where do the keeps live?
        -> {mode: mark|hop|home, location, in_place, moved}.
        `mark` = the sandbox itself is durable — zero bytes touched;
        `hop` = one site-side move to the declared durable path;
        `home` = transfer to the controller (only ever EXPLICIT:
        dest='@workspace' or a controller path). No durable + no dest
        → retain.no_durable with the levers in the hints."""
        if layout not in ("target", "label"):
            raise WeftError("task.invalid",
                            f"unknown layout {layout!r}", stage="infra",
                            hints={"known": ["target", "label"]})
        if layout == "label" and not label:
            raise WeftError("task.invalid",
                            "layout='label' needs a label", stage="infra")
        sub = f"runs/{label}/{target}" if layout == "label" \
            else f"runs/{target}"
        if dest is not None:
            if dest == "@workspace":
                return {"mode": "home", "in_place": False, "moved": True,
                        "location": str(self.workspace / sub)}
            return {"mode": "home", "in_place": False, "moved": True,
                    "location": str(Path(dest))}
        cfg = (self.store.get_site(site) or {}).get("config") or {}
        facts = storage_facts(cfg)
        durable = facts["durable"]
        if durable is True:
            return {"mode": "mark", "in_place": True, "moved": False,
                    "location": adapter.path(jobdir_rel)}
        if isinstance(durable, str):
            return {"mode": "hop", "in_place": True, "moved": True,
                    "location": f"{durable.rstrip('/')}/{sub}"}
        raise WeftError(
            "retain.no_durable",
            f"site {site!r} declares no durable storage — say where "
            f"these files should survive",
            stage="staging",
            hints={"options": {
                       "ship_home": "run_retain(..., dest='@workspace') "
                                    "— transfer to the controller's "
                                    "workspace",
                       "declare": "re-register the site with "
                                  "durable=true (the root is safe) or "
                                  "durable='/abs/path' (a separate safe "
                                  "path on the node)"},
                   **({"registration_hint": facts["hint"]}
                      if facts.get("hint") else {})})

    def _is_finished(self, kind: str, row: dict) -> bool:
        return (kind == "job" and row["state"] in TERMINAL_JOB) or \
            (kind == "kernel" and row["state"] in TERMINAL_KERNEL)

    def retain(self, target: str, include=None, exclude=None,
               dest: str | None = None, max_gb: float | None = None,
               label: str = "", background: bool = True,
               headroom_gb: float = 20.0, layout: str = "target") -> dict:
        if label and len(label) > 200:
            raise WeftError("task.invalid", "label max 200 chars",
                            stage="infra")
        kind, row, jobdir_rel = self._target_row(target)
        site = row["site"]
        adapter = self.adapters.get(site)
        if adapter is None:
            raise WeftError("task.invalid",
                            f"site {site!r} not registered", stage="infra",
                            hints={"suggestion": "register_site first — "
                                                 "this is not an outage"})
        # the placement decision is made NOW — a pin on a no-durable
        # site must ask its question at retain time, not at settlement
        place = self._placement(target, site, jobdir_rel, dest, label,
                                layout, adapter)
        location, in_place = place["location"], place["in_place"]
        selection = {"include": include, "exclude": exclude, "dest": dest,
                     "max_gb": max_gb, "headroom_gb": headroom_gb,
                     "layout": layout}
        selected = self._select(self._scan(adapter, jobdir_rel),
                                include, exclude)

        if not self._is_finished(kind, row):
            # LIVE target. Completed-block-only selections settle now
            # (immutable by protocol); anything else — INCLUDING files
            # that don't exist yet ("retain the eventual complete
            # file") — becomes a PIN: decide now, settle at run end.
            immediate_ok = False
            if selected and place["mode"] != "mark":
                try:
                    self._guard_finished(kind, row, selected, adapter)
                    immediate_ok = True
                except WeftError:
                    pass
            if not immediate_ok:
                self.store.put_retained(target, site, label or None,
                                        location, in_place, 0, 0,
                                        state="pinned-pending",
                                        selection=selection,
                                        moved=place["moved"])
                self.store.emit("retain.pinned", target=target, site=site,
                                matched_now=len(selected),
                                **({"label": label} if label else {}))
                return {"target": target, "state": "pinned-pending",
                        "matched_now": len(selected),
                        "moved": place["moved"],
                        "location": {"site": site if in_place
                                     else "@workspace", "path": location},
                        "note": "settled when the run ends (stop/death/"
                                "completion); run_forget cancels the pin"}

        if not selected:
            raise WeftError("data.missing",
                            "selection matched no files", stage="staging",
                            hints={"include": include, "exclude": exclude})
        total = sum(e["bytes"] for e in selected)

        if place["mode"] == "mark":
            # retention2.md: the sandbox IS durable — pin, move nothing.
            # No budgets (zero new bytes), no background (instant).
            self.store.put_retained(target, site, label or None, location,
                                    in_place, len(selected), total,
                                    state="done", selection=selection,
                                    moved=False)
            self.store.update_retained(target, method="mark")
            self._sidecar_into(adapter, jobdir_rel, target, site, selected)
            self._link_keeps(target, kind, row, site, selected,
                             location, True, False)
            self.store.emit("retain.marked", target=target, site=site,
                            files=len(selected), bytes=total,
                            **({"label": label} if label else {}))
            return {"target": target, "files": len(selected),
                    "bytes": total, "moved": False, "in_place": True,
                    "state": "done",
                    "location": {"site": site, "path": location},
                    "note": "durable root: marked in place — nothing "
                            "moved; paths stay valid",
                    **({"label": label} if label else {})}

        self._check_budgets(total, len(selected), max_gb, headroom_gb,
                            location, in_place)
        self.store.put_retained(target, site, label or None, location,
                                in_place, len(selected), total,
                                state="queued", selection=selection,
                                moved=True)

        def work():
            self._capture(target, kind, row, jobdir_rel, adapter, site,
                          selected, location, in_place, label)

        if background:
            threading.Thread(target=work, daemon=True).start()
            state = "queued"
        else:
            work()
            state = self.store.get_retained(target)["state"]
        return {"target": target, "files": len(selected), "bytes": total,
                "moved": True,
                "location": {"site": site if in_place else "@workspace",
                             "path": location},
                "in_place": in_place, "state": state,
                **({"label": label} if label else {})}

    def _check_budgets(self, total, nfiles, max_gb, headroom_gb,
                       location, in_place) -> None:
        if max_gb is not None and total > max_gb * 1e9:
            raise WeftError(
                "task.invalid",
                f"selection is {total/1e9:.2f} GB, over the "
                f"{max_gb} GB cap", stage="staging",
                hints={"files": nfiles,
                       "suggestion": "narrow include/exclude or raise "
                                     "max_gb"})
        if not in_place:
            free = shutil.disk_usage(
                Path(location).parent if Path(location).parent.exists()
                else self.workspace).free
            if free - total < (headroom_gb or 20.0) * 1e9:
                raise WeftError(
                    "quota.storage",
                    f"retaining {total/1e9:.2f} GB would leave "
                    f"{(free-total)/1e9:.2f} GB free (< {headroom_gb} GB "
                    "headroom)", stage="staging",
                    hints={"free_gb": round(free / 1e9, 2),
                           "suggestion": "narrow the selection or lower "
                                         "policy headroom deliberately"})

    def _capture(self, target, kind, row, jobdir_rel, adapter, site,
                 selected, location, in_place, label) -> None:
        """Place + sidecar + row update, with honest failure. Runs on
        the caller's thread (immediate work()) or a settlement hook."""
        local_like = adapter.__class__.__name__ == "LocalAdapter" or \
            getattr(adapter, "transport", "ssh") == "local"
        total = sum(e["bytes"] for e in selected)
        try:
            self.store.update_retained(target, state="inflight")
            method = self._place(adapter, jobdir_rel, selected,
                                 location, in_place, local_like)
            self._sidecar(kind, row, target, site, label, selected,
                          location, in_place, method, adapter)
            self.store.update_retained(target, state="done",
                                       method=method, files=len(selected),
                                       nbytes=total)
            self._link_keeps(target, kind, row, site, selected,
                             location, in_place, True)
            self.store.emit("retain.done", target=target, site=site,
                            files=len(selected), bytes=total,
                            method=method, location=location,
                            **({"label": label} if label else {}))
        except WeftError as e:
            self.store.update_retained(target, state="failed",
                                       error=json.dumps(e.to_dict()))
            self.store.emit("retain.failed", target=target, **e.to_dict())
        except Exception as e:  # noqa: BLE001 — surfaced, not raised
            self.store.update_retained(
                target, state="failed",
                error=json.dumps({"error": "internal.error",
                                  "detail": repr(e)}))
            self.store.emit("retain.failed", target=target,
                            error="internal.error", detail=repr(e))

    def settle_pins(self, target: str) -> None:
        """Capture a pinned-pending retain at run settlement (stop,
        death, completion). Call sites guarantee the run is finished —
        the state-machine guard is theirs, not re-checked here. Literal
        pinned paths that never materialized are reported, honestly,
        while the rest capture."""
        try:
            prow = self.store.get_retained(target)
            if not prow or prow["state"] != "pinned-pending":
                return
            kind, row, jobdir_rel = self._target_row(target)
            adapter = self.adapters.get(row["site"])
            if adapter is None:
                return
            sel = json.loads(prow.get("selection") or "{}")
            include, exclude = sel.get("include"), sel.get("exclude")
            selected = self._select(self._scan(adapter, jobdir_rel),
                                    include, exclude)
            # a literal include can be a FILE or a DIRECTORY-as-a-unit
            # (e.g. a .zarr): present when any selected path IS it or
            # lives under it — else a captured directory would still
            # report pin_missing (found by the aba viewer questions)
            missing = [p for p in (include or [])
                       if not any(ch in p for ch in "*?[")
                       and not any(e["path"] == p or e["path"].startswith(
                           p.rstrip("/") + "/") for e in selected)]
            if missing:
                self.store.emit("retain.pin_missing", target=target,
                                paths=missing,
                                note="pinned paths never materialized")
            if not selected:
                self.store.update_retained(
                    target, state="failed",
                    error=json.dumps({"error": "data.missing",
                                      "detail": "no pinned file existed "
                                                "at settlement",
                                      "missing": missing}))
                return
            if prow.get("moved") == 0:
                # mark-mode pin (durable root): settle = validate + pin,
                # no capture — the files already sit where they'll stay
                total = sum(e["bytes"] for e in selected)
                self.store.update_retained(target, state="done",
                                           method="mark",
                                           files=len(selected),
                                           nbytes=total)
                self._sidecar_into(adapter, jobdir_rel, target,
                                   row["site"], selected)
                self.store.emit("retain.marked", target=target,
                                site=row["site"], files=len(selected),
                                bytes=total,
                                **({"label": prow["label"]}
                                   if prow.get("label") else {}))
                return
            self._check_budgets(sum(e["bytes"] for e in selected),
                                len(selected), sel.get("max_gb"),
                                sel.get("headroom_gb") or 20.0,
                                prow["location"], bool(prow["in_place"]))
            self._capture(target, kind, row, jobdir_rel, adapter,
                          row["site"], selected, prow["location"],
                          bool(prow["in_place"]), prow["label"] or "")
        except Exception as e:  # settlement must never break run teardown
            try:
                self.store.emit("retain.failed", target=target,
                                error="internal.error", detail=repr(e))
            except Exception:
                pass

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

    # -- sandbox file access (preview tier; aba Files panel) ------------------

    _READ_HARD_CAP = 8 << 20   # a preview channel, not a transport

    def _sandbox_path(self, target: str, rel: str) -> tuple:
        """(adapter, abs_path) with the path CONFINED to the jobdir —
        a '../' escape is refused, never resolved."""
        kind, row, jobdir_rel = self._target_row(target)
        adapter = self.adapters.get(row["site"])
        if adapter is None:
            raise WeftError("task.invalid",
                            f"site {row['site']!r} not registered",
                            stage="infra",
                            hints={"suggestion": "register_site first — "
                                                 "this is not an outage"})
        root = adapter.path(jobdir_rel)
        joined = os.path.normpath(os.path.join(root, rel))
        if not joined.startswith(root.rstrip("/") + "/"):
            raise WeftError("task.invalid",
                            f"path escapes the run sandbox: {rel!r}",
                            stage="infra")
        return adapter, joined

    def resolve_key(self, target: str, rel: str) -> list[dict]:
        """(run, relpath) — retention2.md's durable handle — resolved to
        every place the bytes might currently be, in precedence order:
        the sandbox, then the keep (only once its row is `done`).
        Each candidate: {at, adapter|None, path} (adapter None = a
        local/workspace path read directly)."""
        adapter, sandbox = self._sandbox_path(target, rel)
        out = [{"at": "sandbox", "adapter": adapter, "path": sandbox}]
        row = self.store.get_retained(target)
        if row and row["state"] == "done" and row.get("moved") != 0:
            # a MOVED keep adds a second address; a MARK's keep address
            # IS the sandbox path (already listed)
            norm = os.path.normpath(os.path.join(row["location"], rel))
            if norm.startswith(os.path.normpath(row["location"])):
                out.append({"at": "retained",
                            "adapter": adapter if row["in_place"]
                            else None,
                            "path": norm})
        return out

    @staticmethod
    def _stat_one(cand: dict) -> dict | None:
        path = cand["path"]
        if cand["adapter"] is None:
            p = Path(path)
            if not p.is_file():
                return None
            st = p.stat()
            return {"bytes": st.st_size, "mtime": int(st.st_mtime)}
        r = cand["adapter"].run_cmd(
            f'[ -f {shlex.quote(path)} ] && '
            f'(stat -c "%s %Y" {shlex.quote(path)} 2>/dev/null || '
            f'stat -f "%z %m" {shlex.quote(path)}) || echo ABSENT',
            timeout=60)
        out = (r.out or "").strip()
        if r.rc != 0 or out == "ABSENT" or not out:
            return None
        parts = out.split()
        return {"bytes": int(parts[0]), "mtime": int(parts[1])}

    def file_stat(self, target: str, rel: str) -> dict:
        for cand in self.resolve_key(target, rel):
            st = self._stat_one(cand)
            if st is not None:
                return {"target": target, "path": rel, "exists": True,
                        "at": cand["at"], "abs_path": cand["path"], **st}
        return {"target": target, "path": rel, "exists": False}

    def file_read(self, target: str, rel: str,
                  max_bytes: int = 1 << 20) -> dict:
        max_bytes = min(max_bytes, self._READ_HARD_CAP)
        for cand in self.resolve_key(target, rel):
            st = self._stat_one(cand)
            if st is None:
                continue
            if cand["adapter"] is None:
                data = Path(cand["path"]).open("rb").read(max_bytes)
                import base64 as _b64
                b64 = _b64.b64encode(data).decode()
            else:
                r = cand["adapter"].run_cmd(
                    f"head -c {max_bytes} {shlex.quote(cand['path'])} "
                    f"| base64", timeout=300)
                if r.rc != 0:
                    raise WeftError("data.missing",
                                    f"read failed: {(r.err or '')[:200]}",
                                    stage="infra", retryable=True)
                b64 = "".join(r.out.split())
            return {"target": target, "path": rel, "at": cand["at"],
                    "bytes_b64": b64, "bytes_total": st["bytes"],
                    "truncated": st["bytes"] > max_bytes}
        raise WeftError("data.missing",
                        f"no such file in the run sandbox or its keep: "
                        f"{rel}",
                        stage="infra",
                        hints={"note": "run_inventory shows what the "
                                       "run produced; the sandbox may "
                                       "have been swept and the keep "
                                       "forgotten"})

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
            raise WeftError("task.invalid",
                            f"site {row['site']!r} not registered",
                            stage="infra",
                            hints={"suggestion": "register_site first — "
                                                 "this is not an outage"})
        # a discard must never destroy what a pin promised: capture first
        self.settle_pins(target)
        kept = self.store.get_retained(target)
        if kept and kept.get("moved") == 0 and kept["state"] == "done":
            # a MARKED keep lives IN this sandbox: selective discard —
            # the junk goes, the keeps (and sidecar) stay, the pin holds
            sel = json.loads(kept.get("selection") or "{}")
            entries = self._scan(adapter, jobdir_rel)
            keep_paths = {e["path"] for e in self._select(
                entries, sel.get("include"), sel.get("exclude"))}
            keep_paths.add(".weft-run.json")
            doomed = [e["path"] for e in entries
                      if e["path"] not in keep_paths]
            if doomed:
                root = adapter.path(jobdir_rel)
                script = [f"cd {shlex.quote(root)} || exit 1"]
                script += [f"rm -f {shlex.quote(p)}" for p in doomed]
                # empty dirs left behind are litter, not keeps
                script.append("find . -type d -empty -delete "
                              "2>/dev/null; true")
                adapter.run_cmd("\n".join(script), timeout=1800)
            self.store.emit("run.discarded", target=target,
                            site=row["site"], selective=True,
                            removed=len(doomed))
            return {"target": target, "state": "discarded",
                    "selective": True, "removed": len(doomed),
                    "kept": len(keep_paths) - 1,
                    "note": "marked-in-place keep: unselected files "
                            "deleted, the keeps stay at their paths; "
                            "run_forget first for full deletion"}
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
            if row["state"] == "pinned-pending":
                # nothing placed yet: forgetting a pending pin CANCELS it
                self.store.delete_retained(row["target"])
                self.store.emit("retain.pin_cancelled",
                                target=row["target"])
                forgotten.append({"target": row["target"], "bytes": 0,
                                  "location": None,
                                  "note": "pin cancelled before capture"})
                continue
            if row["state"] in ("queued", "inflight"):
                # deleting a destination WHILE a transfer writes into it
                # would race — refuse until the retain settles
                pending.append({"target": row["target"],
                                "why": "retain.in_flight",
                                "retryable": True})
                continue
            if row.get("moved") == 0:
                # retention2.md: forget is the INVERSE of retain, and a
                # MARK created no copies — so forget deletes NOTHING.
                # Drop the pin (+ sidecar); files revert to ordinary
                # sandbox lifecycle (run_discard destroys bytes).
                adapter = self.adapters.get(row["site"])
                if adapter is not None:
                    adapter.run_cmd(
                        f"rm -f {shlex.quote(row['location'])}"
                        f"/.weft-run.json", timeout=120)
                self.store.delete_retained(row["target"])
                self.store.emit("retain.forgotten", target=row["target"],
                                bytes=0, unmarked=True)
                forgotten.append({"target": row["target"], "bytes": 0,
                                  "location": row["location"],
                                  "note": "unmarked — files remain in "
                                          "the sandbox (run_discard "
                                          "deletes bytes)"})
                continue
            try:
                if row["in_place"]:
                    adapter = self.adapters.get(row["site"])
                    if adapter is None:
                        raise WeftError("task.invalid",
                                        f"site {row['site']!r} not "
                                        "registered", stage="infra",
                                        hints={"suggestion":
                                               "register_site first — "
                                               "this is not an outage"})
                    r = adapter.run_cmd(
                        f"rm -rf {shlex.quote(row['location'])}",
                        timeout=1800)
                    if r.rc != 0:
                        raise WeftError("data.transfer_failed",
                                        f"delete failed: {r.err[:200]}",
                                        stage="infra",
                                        hints={"suggestion":
                                               "permissions or a busy "
                                               "file at the retained "
                                               "path; not an outage"})
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

    def _link_keeps(self, target: str, kind: str, row: dict, site: str,
                    selected, location: str, in_place: bool,
                    moved: bool) -> None:
        """retention2.md LINK: keeps of DECLARED outputs anchor their
        refs — the manifest already paid for the hash, so recording
        {keep address} on the dataref costs nothing and lets fetch and
        staging re-obtain the bytes from the keep after cache eviction.
        Undeclared files stay lazy (identity at first computational
        use). Best-effort by contract: a LINK failure never fails a
        retain."""
        try:
            if kind != "job":
                return                     # kernels declare nothing
            outputs = (row.get("manifest") or {}).get("outputs") or []
            by_path = {o["path"]: o["ref"] for o in outputs
                       if o.get("ref") and not o["path"].endswith("/")}
            for e in selected:
                ref = by_path.get(e["path"])
                if not ref:
                    continue
                keep_path = f"{location.rstrip('/')}/{e['path']}" \
                    if moved else f"{location.rstrip('/')}/{e['path']}"
                self.store.update_dataref_meta(ref, {"keep": {
                    "target": target, "rel": e["path"],
                    "site": site if in_place else "@workspace",
                    "path": keep_path,
                    "bytes": e["bytes"], "mtime": e["mtime"]}})
        except Exception:   # noqa: BLE001 — LINK is opportunistic
            pass

    def _sidecar_into(self, adapter, jobdir_rel, target, site,
                      selected) -> None:
        """Sidecar for a MARKED keep: written into the jobdir itself —
        the directory self-describes without weft, at its own path."""
        kind, row, _ = self._target_row(target)
        retained = self.store.get_retained(target) or {}
        self._sidecar(kind, row, target, site, retained.get("label"),
                      selected, adapter.path(jobdir_rel), True, "mark",
                      adapter)

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
