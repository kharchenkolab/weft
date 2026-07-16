"""Data manager: DataRef registration, staging plans, transfers, collection.

Staging is a set difference over the location table (doc 04 §3): required
refs minus refs present at the target, compiled into blob transfers. On
collection, outputs are hashed *at the site* and ingested into the site
CAS before anything moves — task chaining then finds them already present.
"""

from __future__ import annotations

import time
from pathlib import Path

from .adapters.base import SiteAdapter
from .cas import LocalCAS, StagingPlan, staging_plan
from .errors import WeftError
from .ids import canonical_json, sha256_bytes
from .preview import preview_for
from .store import Store
from .task import Task

PREVIEW_HEAD_BYTES = 32 * 1024
LOG_TAIL_LINES = 100


class DataManager:
    def __init__(self, store: Store, cas: LocalCAS, workspace: Path):
        self.store = store
        self.cas = cas
        self.workspace = Path(workspace)

    # -- registration ------------------------------------------------------

    def register(self, path: str | Path, origin: str | None = None) -> dict:
        info = self.cas.register(Path(path))
        meta = {"origin": origin or str(path), "exec": info.exec}
        if info.plain_sha256:
            meta["sha256_plain"] = info.plain_sha256
        self.store.put_dataref(info.ref, info.kind, info.bytes, info.chunks,
                               meta=meta)
        # "@workspace" is a reserved pseudo-site: the local CAS itself
        self.store.set_location(info.ref, "@workspace", str(self.cas.root))
        return {"ref": info.ref, "kind": info.kind, "bytes": info.bytes}

    def register_site_path(self, adapter, site: str, abs_path: str,
                           origin: str | None = None,
                           expected_sha256: str | None = None) -> dict:
        """A file — or a whole DIRECTORY (a .zarr, a results folder) —
        already ON a site becomes a ref without moving: hashed
        site-side, hardlinked into the site CAS (copy across devices).
        The browsable original stays; reuse on that site stages 0 bytes
        (retention.md R5). Directories mint a tree ref (same convention
        as output collection, so identical content re-registered mints
        identical refs and only new blobs are placed)."""
        import shlex as _sh
        q = _sh.quote(abs_path)
        probe = adapter.run_cmd(
            f"if [ -d {q} ]; then echo dir; "
            f"elif [ -f {q} ]; then echo file; else echo none; fi",
            timeout=120)
        kind = (probe.out or "").strip()
        if kind == "dir":
            return self._register_site_tree(adapter, site, abs_path,
                                            origin, expected_sha256)
        if kind != "file":
            raise WeftError("data.missing",
                            f"no such file or directory on {site}: "
                            f"{abs_path}",
                            stage="staging",
                            hints={"detail": (probe.err or probe.out)[-200:]})
        r = adapter.run_cmd(
            f"h=$(sha256sum {q} 2>/dev/null || shasum -a 256 {q}); "
            f"printf '%s %s' \"${{h%% *}}\" \"$(wc -c < {q} | tr -d ' ')\"",
            timeout=1800)
        parts = (r.out or "").split()
        if r.rc != 0 or len(parts) < 2:
            raise WeftError("data.missing",
                            f"no such file on {site}: {abs_path}",
                            stage="staging",
                            hints={"detail": (r.err or r.out)[-200:]})
        digest, size = parts[0], int(parts[1])
        self._check_expected(abs_path, digest, expected_sha256)
        endpoint = adapter.transfer_endpoint()
        blob_dir = f"{endpoint['cas_root']}/{digest[:2]}"
        blob = f"{blob_dir}/{digest}"
        r = adapter.run_cmd(
            f"mkdir -p {_sh.quote(blob_dir)} && "
            f"([ -f {_sh.quote(blob)} ] || ln {q} {_sh.quote(blob)} "
            f"2>/dev/null || cp -p {q} {_sh.quote(blob)})", timeout=1800)
        if r.rc != 0:
            raise WeftError("data.transfer_failed",
                            f"site-side ingest failed: {r.err[:200]}",
                            stage="staging", retryable=True)
        ref = f"dref:{digest}"
        trust = "verified" if expected_sha256 else "first-fetch"
        self.store.put_dataref(ref, "file", size,
                               meta={"origin": origin or abs_path,
                                     "trust": trust, "site_direct": site})
        self.store.set_location(ref, site, endpoint["cas_root"])
        return {"ref": ref, "kind": "file", "bytes": size,
                "fetched_to": site, "trust": trust,
                "note": "bytes live in the site CAS (the original stays "
                        "in place); tasks on this site stage 0 bytes"}

    def _register_site_tree(self, adapter, site: str, abs_path: str,
                            origin: str | None,
                            expected_sha256: str | None) -> dict:
        """Directory-as-a-unit registration (retention re-entry for
        .zarr-style stores): one shim hash-tree call hashes every file
        site-side, one shim ingest call hardlinks the blobs into the
        site CAS, and the manifest is adopted locally so tasks can
        mount the tree and data_fetch can pull it home. The tree hash
        follows the output-collection convention exactly — identity is
        content, wherever it was minted."""
        import shlex as _sh
        import uuid as _uuid
        listing = adapter.shim(["hash-tree", "--root", abs_path],
                               timeout=3600)
        if listing.rc != 0:
            raise WeftError(
                "data.missing",
                f"cannot hash {abs_path} on {site}: "
                f"{(listing.err or listing.out)[:200]}",
                stage="staging", retryable=True)
        tree_entries, ingest_rows, total = [], [], 0
        for line in listing.out.splitlines():
            kind, path, is_exec, size, digest = line.split("\t")
            if kind == "link":
                tree_entries.append({"path": path, "kind": "link",
                                     "target": digest})
                continue
            size = int(size)
            tree_entries.append({"path": path, "kind": "file",
                                 "exec": is_exec == "1",
                                 "size": size, "sha256": digest})
            ingest_rows.append(f"{path}\t{digest}")
            total += size
        if not ingest_rows:
            raise WeftError(
                "data.missing",
                f"{abs_path} on {site} contains no files — nothing to "
                f"register", stage="staging")
        tree_hash = sha256_bytes(canonical_json(tree_entries))
        self._check_expected(abs_path, tree_hash, expected_sha256)
        endpoint = adapter.transfer_endpoint()
        plan_rel = f"tmp/ingest-{_uuid.uuid4().hex[:10]}.tsv"
        adapter.write_file(plan_rel,
                           ("\n".join(ingest_rows) + "\n").encode())
        r = adapter.shim(
            ["ingest", "--cas", endpoint["cas_root"], "--root", abs_path,
             "--plan", adapter.path(plan_rel)], timeout=3600)
        adapter.run_cmd(f"rm -f {_sh.quote(adapter.path(plan_rel))}",
                        timeout=120)
        if r.rc != 0:
            raise WeftError("data.transfer_failed",
                            f"site-side tree ingest failed: "
                            f"{(r.err or r.out)[:200]}",
                            stage="staging", retryable=True)
        ref = f"dref:{tree_hash}"
        trust = "verified" if expected_sha256 else "first-fetch"
        self.cas.put_tree_manifest(tree_hash, tree_entries)
        self.store.put_dataref(ref, "tree", total,
                               meta={"origin": origin or abs_path,
                                     "trust": trust, "site_direct": site})
        self.store.set_location(ref, site, endpoint["cas_root"])
        nfiles = 0
        for e in tree_entries:
            if e["kind"] != "file":
                continue
            nfiles += 1
            fref = f"dref:{e['sha256']}"
            self.store.put_dataref(fref, "file", e["size"],
                                   meta={"origin": origin or abs_path,
                                         "exec": e["exec"]})
            self.store.set_location(fref, site, endpoint["cas_root"])
        return {"ref": ref, "kind": "tree", "bytes": total,
                "files": nfiles, "fetched_to": site, "trust": trust,
                "note": "blobs live in the site CAS (the original tree "
                        "stays in place); tasks on this site stage 0 "
                        "bytes; data_fetch(ref, dest) rebuilds the "
                        "directory elsewhere"}

    def register_url(self, url: str, fetchers: dict, adapters: dict,
                     site: str | None = None,
                     expected_sha256: str | None = None) -> dict:
        """Ingest a remote artifact into the data plane (design A2)."""
        scheme = url.split("://", 1)[0].lower()
        fetcher = fetchers.get(scheme)
        if fetcher is None:
            raise WeftError(
                "task.invalid", f"no fetcher for scheme {scheme!r}",
                stage="staging",
                hints={"registered": sorted(set(fetchers)),
                       "suggestion": "https URLs work everywhere"},
            )
        trust = "verified" if expected_sha256 else "first-fetch"

        if site is None:
            tmp = self.cas.root / "tmp-ingest"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            digest = fetcher.fetch_to_file(url, tmp)
            self._check_expected(url, digest, expected_sha256)
            info = self.cas.register_file(tmp)
            tmp.unlink(missing_ok=True)
            self.store.put_dataref(info.ref, "file", info.bytes, info.chunks,
                                   meta={"origin": url, "trust": trust,
                                         **({"sha256_plain": info.plain_sha256}
                                            if info.plain_sha256 else {})})
            self.store.set_location(info.ref, "@workspace", str(self.cas.root))
            return {"ref": info.ref, "kind": "file", "bytes": info.bytes,
                    "fetched_to": "@workspace", "trust": trust}

        adapter = adapters.get(site)
        if adapter is None:
            raise WeftError("task.invalid", f"unknown site: {site}",
                            stage="staging",
                            hints={"registered": sorted(adapters)})
        import uuid as _uuid
        tmp_rel = f"tmp/ingest-{_uuid.uuid4().hex[:10]}"
        fetcher.fetch_on_site(adapter, url, adapter.path(tmp_rel))
        out = adapter.run_cmd(
            f"sha256sum {adapter.path(tmp_rel)} | cut -d' ' -f1 && "
            f"wc -c < {adapter.path(tmp_rel)}", timeout=600)
        parts = out.out.split()
        if out.rc != 0 or len(parts) < 2:
            raise WeftError("data.transfer_failed",
                            "site-side hash of fetched artifact failed",
                            stage="staging", retryable=True,
                            hints={"detail": (out.err or out.out)[-300:]})
        digest, size = parts[0], int(parts[1])
        self._check_expected(url, digest, expected_sha256, adapter, tmp_rel)
        endpoint = adapter.transfer_endpoint()
        blob_dir = f"{endpoint['cas_root']}/{digest[:2]}"
        adapter.run_cmd(
            f"mkdir -p {blob_dir} && mv {adapter.path(tmp_rel)} "
            f"{blob_dir}/{digest}")
        ref = f"dref:{digest}"
        self.store.put_dataref(ref, "file", size,
                               meta={"origin": url, "trust": trust,
                                     "site_direct": site})
        self.store.set_location(ref, site, endpoint["cas_root"])
        return {"ref": ref, "kind": "file", "bytes": size,
                "fetched_to": site, "trust": trust,
                "note": "bytes live in the site CAS; data_fetch brings "
                        "them to the workspace if ever needed"}

    def _check_expected(self, url, digest, expected, adapter=None,
                        tmp_rel=None) -> None:
        if expected and digest != expected:
            if adapter is not None and tmp_rel:
                adapter.run_cmd(f"rm -f {adapter.path(tmp_rel)}")
            raise WeftError(
                "data.verify_failed",
                "fetched content does not match expected_sha256",
                stage="staging",
                hints={"source": url, "expected": expected, "got": digest,
                       "suggestion": "wrong URL/version, or the publisher's "
                                     "checksum is for a different encoding"},
            )

    def describe(self, ref: str) -> dict:
        row = self.store.get_dataref(ref)
        if not row:
            raise WeftError("data.missing", f"unknown ref: {ref}", stage="staging")
        row["locations"] = [
            {"site": loc["site"], "verified_at": loc["verified_at"]}
            for loc in self.store.locations_of(ref)
        ]
        return row

    # -- staging ------------------------------------------------------------

    def sizes(self, refs: list[str]) -> dict[str, int]:
        out = {}
        for r in refs:
            row = self.store.get_dataref(r)
            out[r] = row["bytes"] if row else 0
        return out

    def plan_for(self, refs: list[str], site: str) -> StagingPlan:
        return staging_plan(refs, self.store.refs_present_at(site), self.sizes(refs))

    def blobs_for(self, ref: str) -> list[tuple[str, int]]:
        """(cas-name, size) of every blob backing a ref."""
        kind = self.cas.kind_of(ref)
        if kind is None:
            raise WeftError(
                "data.missing", f"ref content not in local CAS: {ref}", stage="staging"
            )
        if kind == "file":
            row = self.store.get_dataref(ref)
            return [(ref.split(":")[-1], row["bytes"] if row else 0)]
        return [
            (e["sha256"], e["size"])
            for e in self.cas.tree_manifest(ref)
            if e["kind"] == "file"
        ]

    def verify_map_for(self, refs: list[str]) -> dict[str, str | None]:
        """cas-name -> content sha256 usable by remote `sha256sum -c`.

        For chunked blobs the CAS name is a merkle root; the plain hash
        travels in metadata. None = cannot verify remotely by content
        (legacy refs registered before plain hashes were recorded)."""
        out: dict[str, str | None] = {}
        for ref in refs:
            row = self.store.get_dataref(ref)
            if row and row["kind"] == "file":
                name = ref.split(":")[-1]
                if row["chunks"]:
                    out[name] = row["meta"].get("sha256_plain")
                else:
                    out[name] = name
            elif self.cas.kind_of(ref) == "tree":
                for e in self.cas.tree_manifest(ref):
                    if e["kind"] == "file":
                        out[e["sha256"]] = e.get("sha256_plain", e["sha256"]) \
                            if "sha256_plain" in e or e["size"] < 64 * 1024 * 1024 \
                            else None
        return out

    def _blob_names_for(self, ref: str) -> list[tuple[str, int]]:
        """Like blobs_for, but works when the CONTENT is absent from the
        workspace CAS (only the record exists) — site-to-site routing
        moves bytes the controller never held."""
        row = self.store.get_dataref(ref)
        if row and row.get("chunks"):
            n = len(row["chunks"])
            per = (row["bytes"] or 0) // max(n, 1)
            return [(c, per) for c in row["chunks"]]
        if self.cas.kind_of(ref) == "tree":
            return [(e["sha256"], e["size"])
                    for e in self.cas.tree_manifest(ref)
                    if e["kind"] == "file"]
        return [(ref.split(":")[-1], (row or {}).get("bytes", 0) or 0)]

    def _route_from_sites(self, missing: list[str], adapter: SiteAdapter,
                          adapters: dict, job_id: str | None) -> list[str]:
        """Site-to-site staging: for refs the workspace CAS does not hold,
        move bytes DIRECTLY from a site that has them — shared-filesystem
        link when dst sees src's root, dst-pulls-over-ssh when the user's
        own keys permit it. Returns the refs it satisfied; the rest fall
        through to the workspace path."""
        import shlex as _sh
        done: list[str] = []
        for ref in missing:
            if self.cas.kind_of(ref) == "file":
                continue      # workspace holds the bytes: normal path wins
            row = self.store.get_dataref(ref)
            if row is None:
                continue
            if row.get("kind") == "tree":
                if self.cas.kind_of(ref) is None:
                    continue  # tree manifest unknown: cannot enumerate
                if all(self.cas._blob_path(e["sha256"]).exists()
                       for e in self.cas.tree_manifest(ref)
                       if e["kind"] == "file"):
                    continue  # workspace holds every member: normal path
            sources = [l["site"] for l in self.store.locations_of(ref)
                       if l["present"] and l["site"] not in
                       ("@workspace", adapter.name)
                       and l["site"] in adapters]
            for src in sources:
                route = self.store.get_route(src, adapter.name)
                if not route or not (route.get("shared_fs_path")
                                     or route.get("direct_ssh")):
                    continue
                src_adapter = adapters[src]
                src_cas = f"{src_adapter.root}/cas"
                dst_cas = adapter.transfer_endpoint()["cas_root"]
                blobs = self._blob_names_for(ref)
                verify = self.verify_map_for([ref])
                lines = ["set -e"]
                for digest, _size in blobs:
                    d = f"{dst_cas}/{digest[:2]}/{digest}"
                    s = f"{src_cas}/{digest[:2]}/{digest}"
                    lines.append(f"mkdir -p {_sh.quote(dst_cas)}/{digest[:2]}")
                    if route.get("shared_fs_path"):
                        # hardlink when the roots share an inode space;
                        # copy when they are different FSes on one mount view
                        lines.append(
                            f"[ -f {_sh.quote(d)} ] || "
                            f"ln {_sh.quote(s)} {_sh.quote(d)} 2>/dev/null "
                            f"|| cp {_sh.quote(s)} {_sh.quote(d)}")
                    else:
                        addr = route.get("src_addr") or ""
                        if ":" in addr.rsplit("@", 1)[-1]:
                            dest, pnum = addr.rsplit(":", 1)
                            port = f"-p {pnum}"
                        else:
                            dest, port = addr, ""
                        remote = f"{dest}:{src_cas}/{digest[:2]}/{digest}"
                        lines.append(
                            f"[ -f {_sh.quote(d)} ] || "
                            f"rsync -e 'ssh -o BatchMode=yes {port}' "
                            f"{_sh.quote(remote)} {_sh.quote(d)}")
                    want = verify.get(digest)
                    if want:
                        # explicit '-': darwin sha256sum rejects bare
                        # stdin -c (GNU and busybox accept both)
                        lines.append(f"echo {want}  {_sh.quote(d)} "
                                     f"| sha256sum -c - >/dev/null")
                via = "fs-link" if route.get("shared_fs_path") \
                    else "direct-pull"
                r = adapter.run_cmd("\n".join(lines), timeout=3600)
                if r.rc != 0:
                    self.store.emit("transfer.route_failed", job_id=job_id,
                                    site=adapter.name, src=src, via=via,
                                    log_tail=(r.err or r.out)[-300:])
                    continue      # try the next source / fall through
                self.store.set_location(ref, adapter.name, dst_cas)
                self.store.emit("transfer.done", job_id=job_id,
                                site=adapter.name, src=src, via=via,
                                bytes_total=row.get("bytes", 0) or 0,
                                files=len(blobs))
                done.append(ref)
                break
        return done

    def ensure_at(self, refs: list[str], adapter: SiteAdapter, transfers: dict,
                  job_id: str | None = None,
                  adapters: dict | None = None) -> dict:
        """Move missing refs to the site CAS; verify; register locations.
        Refs whose bytes the workspace never held route SITE-TO-SITE first
        (shared-FS link or direct pull over the user's own keys) when a
        probed route exists — the controller stays out of the data path."""
        plan = self.plan_for(refs, adapter.name)
        if plan.to_transfer and adapters:
            routed = self._route_from_sites(list(plan.to_transfer), adapter,
                                            adapters, job_id)
            # controller detour for what no direct route satisfied: pull
            # the blobs home first, then the normal workspace→site path
            # ships them (two hops — the honest fallback, and the plan/
            # events say which route each ref took)
            for ref in plan.to_transfer:
                if ref in routed or self.cas.kind_of(ref) is not None:
                    continue
                row = self.store.get_dataref(ref)
                srcs = [l for l in self.store.locations_of(ref)
                        if l["present"] and l["site"] not in
                        ("@workspace", adapter.name)
                        and l["site"] in adapters]
                if row is None or not srcs or not row.get("bytes"):
                    continue      # nothing fetchable: blobs_for will say so
                src_adapter = adapters[srcs[0]["site"]]
                endpoint = src_adapter.transfer_endpoint()
                method = transfers.get(endpoint["method"])
                if method is None:
                    continue
                names = self._blob_names_for(ref)
                method.fetch(names, self.cas, endpoint)
                self.store.set_location(ref, "@workspace",
                                        str(self.cas.root))
                self.store.emit("transfer.done", job_id=job_id,
                                site="@workspace", src=srcs[0]["site"],
                                via="controller-detour",
                                bytes_total=row.get("bytes", 0) or 0,
                                files=len(names))
            plan = self.plan_for(refs, adapter.name)
        if plan.to_transfer:
            endpoint = adapter.transfer_endpoint()
            method = transfers.get(endpoint["method"])
            if method is None:
                raise WeftError(
                    "data.transfer_failed",
                    f"no transfer method {endpoint['method']!r} configured",
                    stage="staging",
                )
            blobs = []
            for ref in plan.to_transfer:
                blobs.extend(self.blobs_for(ref))
            total = sum(s for _, s in blobs)
            t0 = time.time()
            self.store.emit("transfer.start", job_id=job_id, site=adapter.name,
                            method=endpoint["method"], bytes_total=total,
                            files=len(blobs),
                            **method.estimate(blobs, endpoint))
            last_emit = [0.0]

            def _progress(p: dict) -> None:
                now = time.time()
                if now - last_emit[0] >= 1.0:   # token economy: ≥1s apart
                    last_emit[0] = now
                    elapsed = max(now - t0, 0.01)
                    rate = p.get("bytes_done", 0) / elapsed
                    self.store.emit(
                        "transfer.progress", job_id=job_id, site=adapter.name,
                        bytes_total=total, rate_mbps=round(rate / 1e6, 2),
                        eta_s=round((total - p.get("bytes_done", 0))
                                    / max(rate, 1), 1),
                        **p,
                    )

            method.transfer(blobs, self.cas, endpoint, progress=_progress,
                            verify=self.verify_map_for(plan.to_transfer))
            elapsed = round(time.time() - t0, 2)
            rate = round(total / max(elapsed, 0.01) / 1e6, 2)
            self.store.emit("transfer.done", job_id=job_id, site=adapter.name,
                            bytes_total=total, elapsed_s=elapsed,
                            rate_mbps=rate)
            if total > 1 << 20:   # tiny transfers say nothing about the pipe
                self.store.add_metric(adapter.name, "transfer_mbps", rate)
            for ref in plan.to_transfer:
                self.store.set_location(ref, adapter.name, endpoint["cas_root"])
        return plan.to_dict()

    # -- sandbox materialization --------------------------------------------

    def materialize_plan(self, task: Task) -> str:
        """TSV consumed by `weft-shim materialize` (path, sha/target, flag)."""
        rows: list[str] = []
        mounts = list(task.inputs) + ([task.code] if task.code else [])
        for inp in mounts:
            row = self.store.get_dataref(inp.ref)
            kind = row["kind"] if row else self.cas.kind_of(inp.ref)
            if kind is None:
                raise WeftError(
                    "data.missing", f"unknown ref: {inp.ref}", stage="staging",
                    hints={"suggestion": "register the path first with data.register"},
                )
            if kind == "file":
                # exec-ness travels in the ref's metadata; extension is a
                # fallback for refs registered before we recorded it
                if row and "exec" in row.get("meta", {}):
                    is_exec = 1 if row["meta"]["exec"] else 0
                else:
                    is_exec = 1 if inp.mount_as.endswith((".sh", ".py")) else 0
                rows.append(f"{inp.mount_as}\t{inp.ref.split(':')[-1]}\t{is_exec}")
            else:
                for e in self.cas.tree_manifest(inp.ref):
                    p = f"{inp.mount_as}/{e['path']}"
                    if e["kind"] == "link":
                        rows.append(f"{p}\t{e['target']}\tL")
                    else:
                        rows.append(f"{p}\t{e['sha256']}\t{1 if e.get('exec') else 0}")
        return "\n".join(rows) + ("\n" if rows else "")

    # -- collection -----------------------------------------------------------

    def collect_outputs(
        self, adapter: SiteAdapter, jobdir_rel: str, task: Task
    ) -> tuple[list[dict], int]:
        """Hash declared outputs site-side, ingest into site CAS, build
        manifest entries with previews. Returns (entries, total_bytes)."""
        entries: list[dict] = []
        ingest_rows: list[str] = []
        total = 0
        for out in task.outputs:
            out = out.rstrip("/")
            listing = adapter.shim(
                ["hash-tree", "--root", adapter.path(f"{jobdir_rel}/{out}")],
                timeout=600,
            )
            if listing.rc != 0:
                # a declared output that was never produced is a task failure
                raise WeftError(
                    "job.nonzero_exit",
                    f"declared output {out!r} was not produced (no file or "
                    f"directory at that sandbox path)",
                    stage="collecting",
                    hints={"declared_outputs": task.outputs,
                           "detail": listing.err[:300]},
                )
            rows = listing.out.splitlines()
            # single row at "." = the declared output IS a file (the most
            # common step output); it becomes a plain file entry — no tree,
            # no mkdir boilerplate in the task
            if len(rows) == 1 and rows[0].split("\t")[1] == ".":
                _kind, _p, is_exec, size, digest = rows[0].split("\t")
                size = int(size)
                file_ref = f"dref:{digest}"
                self.store.put_dataref(
                    file_ref, "file", size,
                    meta={"origin": f"job:{jobdir_rel}",
                          "exec": is_exec == "1"},
                )
                ingest_rows.append(f"{out}\t{digest}")
                total += size
                entries.append(
                    self._entry(adapter, jobdir_rel, out, file_ref, size))
                continue
            tree_entries = []
            for line in listing.out.splitlines():
                kind, path, is_exec, size, digest = line.split("\t")
                if kind == "link":
                    tree_entries.append({"path": path, "kind": "link", "target": digest})
                    continue
                size = int(size)
                tree_entries.append({
                    "path": path, "kind": "file", "exec": is_exec == "1",
                    "size": size, "sha256": digest,
                })
                ingest_rows.append(f"{out}/{path}\t{digest}")
                total += size
                file_ref = f"dref:{digest}"
                self.store.put_dataref(
                    file_ref, "file", size,
                    meta={"origin": f"job:{jobdir_rel}", "exec": is_exec == "1"},
                )
                entries.append(self._entry(adapter, jobdir_rel, f"{out}/{path}", file_ref, size))
            tree_hash = sha256_bytes(canonical_json(tree_entries))
            tree_ref = f"dref:{tree_hash}"
            # keep the manifest locally so later tasks can mount this tree
            # even though its blobs only exist in the site CAS
            self.cas.put_tree_manifest(tree_hash, tree_entries)
            self.store.put_dataref(
                tree_ref, "tree", sum(e.get("size", 0) for e in tree_entries),
                meta={"origin": f"job:{jobdir_rel}", "path": out},
            )
            entries.append({"path": out + "/", "ref": tree_ref,
                            "bytes": sum(e.get("size", 0) for e in tree_entries),
                            "preview": {"kind": "tree", "files": len(tree_entries)}})
        if ingest_rows:
            plan_rel = f"{jobdir_rel}/ingest.tsv"
            adapter.write_file(plan_rel, ("\n".join(ingest_rows) + "\n").encode())
            endpoint = adapter.transfer_endpoint()
            r = adapter.shim(
                ["ingest", "--cas", endpoint["cas_root"],
                 "--root", adapter.path(jobdir_rel), "--plan", adapter.path(plan_rel)],
                timeout=600,
            )
            if r.rc != 0:
                raise WeftError(
                    "data.verify_failed", f"output ingest failed: {r.err[:300]}",
                    stage="collecting",
                )
            for e in entries:
                self.store.set_location(e["ref"], adapter.name, endpoint["cas_root"])
        return entries, total

    def _entry(
        self, adapter: SiteAdapter, jobdir_rel: str, rel_path: str, ref: str, size: int
    ) -> dict:
        head = b""
        try:
            r = adapter.shim(
                ["head", "--file", adapter.path(f"{jobdir_rel}/{rel_path}"),
                 "--bytes", str(PREVIEW_HEAD_BYTES)], timeout=60,
            )
            head = r.out.encode("utf-8", "surrogateescape") if r.rc == 0 else b""
        except Exception:
            pass
        return {
            "path": rel_path, "ref": ref, "bytes": size,
            "preview": preview_for(rel_path, head, size),
        }

    def fetch(self, ref: str, to_path: str | Path, adapters: dict, transfers: dict) -> dict:
        """Bring a ref's content back to the workspace (doc 05 data.fetch)."""
        dest = Path(to_path)
        if not dest.is_absolute():
            dest = self.workspace / dest
        kind_here = self.cas.kind_of(ref)
        if kind_here == "tree":
            # the manifest is local knowledge; the blobs may not be —
            # pull only what's missing (an evolved re-registered store
            # moves just its new chunks)
            manifest = self.cas.tree_manifest(ref)
            missing = sorted({
                (e["sha256"], e.get("size", 0)) for e in manifest
                if e["kind"] == "file"
                and not self.cas._blob_path(e["sha256"]).exists()})
            if missing:
                locations = self.store.locations_of(ref)
                remote = [l for l in locations if l["site"] in adapters]
                if not remote:
                    raise WeftError(
                        "data.missing",
                        f"no known site holds the blobs of tree {ref}",
                        stage="staging",
                        hints={"missing_blobs": len(missing)})
                adapter = adapters[remote[0]["site"]]
                endpoint = adapter.transfer_endpoint()
                method = transfers.get(endpoint["method"])
                method.fetch(list(missing), self.cas, endpoint)
        elif kind_here is None:
            # find a site that has it and pull blobs back
            locations = self.store.locations_of(ref)
            remote = [l for l in locations if l["site"] in adapters]
            if not remote:
                raise WeftError(
                    "data.missing", f"no known location holds {ref}", stage="staging",
                )
            loc = remote[0]
            adapter = adapters[loc["site"]]
            endpoint = adapter.transfer_endpoint()
            method = transfers.get(endpoint["method"])
            row = self.store.get_dataref(ref)
            if row and row["kind"] == "tree":
                raise WeftError(
                    "data.missing",
                    "this workspace has no manifest for the tree — it can "
                    "enumerate no blobs to pull",
                    stage="staging",
                    hints={"suggestion": "register the tree (data_register"
                                         "(path, site=...)) or import the "
                                         "bundle that minted it first"},
                )
            method.fetch(
                [(ref.split(":")[-1], row["bytes"] if row else 0)], self.cas, endpoint
            )
        # verify + materialize into workspace
        if not self.cas.verify(ref):
            raise WeftError("data.verify_failed", f"content of {ref} failed verification",
                            stage="staging")
        self.cas.materialize(ref, dest, mode="copy")
        return {"ref": ref, "path": str(dest)}
