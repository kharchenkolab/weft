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

    def register(self, path: str | Path) -> dict:
        info = self.cas.register(Path(path))
        meta = {"origin": str(path), "exec": info.exec}
        if info.plain_sha256:
            meta["sha256_plain"] = info.plain_sha256
        self.store.put_dataref(info.ref, info.kind, info.bytes, info.chunks,
                               meta=meta)
        # "@workspace" is a reserved pseudo-site: the local CAS itself
        self.store.set_location(info.ref, "@workspace", str(self.cas.root))
        return {"ref": info.ref, "kind": info.kind, "bytes": info.bytes}

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

    def ensure_at(self, refs: list[str], adapter: SiteAdapter, transfers: dict,
                  job_id: str | None = None) -> dict:
        """Move missing refs to the site CAS; verify; register locations."""
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
                    f"declared output {out!r} missing from sandbox",
                    stage="collecting",
                    hints={"declared_outputs": task.outputs,
                           "detail": listing.err[:300]},
                )
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
        if self.cas.kind_of(ref) is None:
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
                    "tree refs can only be fetched from sites that still hold "
                    "the originating job sandbox; fetch the file refs instead",
                    stage="staging",
                    hints={"suggestion": "fetch individual file refs from the manifest"},
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
