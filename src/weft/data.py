"""Data manager: DataRef registration, staging plans, transfers, collection.

Staging is a set difference over the location table (doc 04 §3): required
refs minus refs present at the target, compiled into blob transfers. On
collection, outputs are hashed *at the site* and ingested into the site
CAS before anything moves — task chaining then finds them already present.
"""

from __future__ import annotations

import os
import re
import shlex
import time
from pathlib import Path

from .adapters.base import SiteAdapter
from .cas import LocalCAS, StagingPlan, staging_plan
from .errors import WeftError
from .ids import canonical_json, sha256_bytes
from .preview import preview_for
from .store import Store
from .task import Task

_HEX64 = re.compile(r"[0-9a-f]{64}")


def _tsv_row(line: str, where: str) -> tuple[str, str, str, int, str]:
    """Strict 5-field hash-tree row. A filename carrying a TAB or
    NEWLINE shreds the row — that is a refusable input, not broken site
    tooling (the shapes it used to surface as: internal.error at
    collect, site.bootstrap_failed from the empty digest)."""
    try:
        kind, path, is_exec, size, digest = line.split("\t")
        return kind, path, is_exec, int(size), digest
    except ValueError:
        raise WeftError(
            "task.invalid",
            f"unparseable listing row from {where} — filenames must not "
            f"contain tab or newline characters",
            stage="staging",
            hints={"row": line[:160],
                   "suggestion": "rename the offending file; weft's "
                                 "transfer plans are tab-separated"})


def _require_digest(digest: str, path: str, site: str) -> str:
    """Identity comes from a real hash, never a fallback: both sha256
    tools failing while wc succeeds would otherwise mint dref:<SIZE> as
    content identity at rc 0 (2026-07 sweep #7)."""
    if not _HEX64.fullmatch(digest or ""):
        raise WeftError(
            "site.bootstrap_failed",
            f"sha256 tooling on {site} produced no digest for {path}",
            stage="staging",
            hints={"got": (digest or "")[:80],
                   "suggestion": "the site needs a working sha256sum "
                                 "(coreutils) or shasum (perl) on PATH — "
                                 "content identity cannot be minted "
                                 "without one"})
    return digest

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
                           expected_sha256: str | None = None,
                           ingest: bool = True) -> dict:
        """A file — or a whole DIRECTORY (a .zarr, a results folder) —
        already ON a site becomes a ref without moving: hashed
        site-side, hardlinked into the site CAS (copy across devices).
        The browsable original stays; reuse on that site stages 0 bytes
        (retention.md R5). Directories mint a tree ref (same convention
        as output collection, so identical content re-registered mints
        identical refs and only new blobs are placed).

        ingest=False — REFERENCE-IN-PLACE, for big data on stable
        storage whose CAS sits on scratch (a cross-filesystem ingest
        would copy it): hash site-side (one read pass, NO write) and
        record the path itself as the ref's durable home. Same-site
        tasks stage it as a SYMLINK (read-only inputs contract — a task
        writing through it damages the HOME, not a re-obtainable cache
        copy); a stat-fence at every staging fails data.verify_failed
        if the home drifted. Bytes are ingested lazily only when they
        must MOVE (cross-site staging / fetch)."""
        import shlex as _sh
        q = _sh.quote(abs_path)
        probe = adapter.run_cmd(
            f"if [ -d {q} ]; then echo dir; "
            f"elif [ -f {q} ]; then echo file; else echo none; fi",
            timeout=120)
        kind = (probe.out or "").strip()
        if kind == "dir":
            return self._register_site_tree(adapter, site, abs_path,
                                            origin, expected_sha256,
                                            ingest=ingest)
        if kind != "file":
            raise WeftError("data.missing",
                            f"no such file or directory on {site}: "
                            f"{abs_path}",
                            stage="staging",
                            hints={"detail": (probe.err or probe.out)[-200:]})
        r = adapter.run_cmd(
            f"h=$(sha256sum {q} 2>/dev/null || shasum -a 256 {q}); "
            f"m=$(stat -c %Y {q} 2>/dev/null || stat -f %m {q}); "
            f"printf '%s %s %s' \"${{h%% *}}\" "
            f"\"$(wc -c < {q} | tr -d ' ')\" \"$m\"",
            timeout=1800)
        parts = (r.out or "").split()
        if r.rc != 0 or len(parts) < 2:
            # the probe just PROVED this path exists — a failure here is
            # unreadability (permissions, vanished mid-flight) or broken
            # site tooling, and saying "no such file" sends the agent
            # chasing a path typo that isn't there (sweep #23)
            raise WeftError("data.missing",
                            f"cannot read {abs_path} on {site} (it exists; "
                            f"permission, tooling, or a mid-flight removal "
                            f"— not a wrong path)",
                            stage="staging",
                            hints={"detail": (r.err or r.out)[-300:]})
        digest, size = parts[0], int(parts[1])
        _require_digest(digest, abs_path, site)
        mtime = int(parts[2]) if len(parts) > 2 else 0
        self._check_expected(abs_path, digest, expected_sha256)
        ref = f"dref:{digest}"
        trust = "verified" if expected_sha256 else "first-fetch"
        if not ingest:
            self.store.put_dataref(
                ref, "file", size,
                meta={"origin": origin or abs_path, "trust": trust,
                      "site_direct": site,
                      "external": {"site": site, "home": abs_path,
                                   "bytes": size, "mtime": mtime}})
            self.store.set_location(ref, site, f"external:{abs_path}")
            self.store.emit("data.external_registered", ref=ref,
                            site=site, home=abs_path, bytes=size)
            return {"ref": ref, "kind": "file", "bytes": size,
                    "external_home": abs_path, "trust": trust,
                    "note": "referenced in place — no copy; same-site "
                            "tasks stage a symlink behind a stat-fence; "
                            "bytes ingest lazily if they ever must move. "
                            "Inputs are read-only BY CONTRACT: writing "
                            "through the symlink damages the home."}
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
                            expected_sha256: str | None,
                            ingest: bool = True) -> dict:
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
            kind, path, is_exec, size, digest = _tsv_row(
                line, f"hash-tree of {abs_path}")
            if kind == "link":
                tree_entries.append({"path": path, "kind": "link",
                                     "target": digest})
                continue
            _require_digest(digest, f"{abs_path}/{path}", site)
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
        trust = "verified" if expected_sha256 else "first-fetch"
        if not ingest:
            ref = f"dref:{tree_hash}"
            self.cas.put_tree_manifest(tree_hash, tree_entries)
            self.store.put_dataref(
                ref, "tree", total,
                meta={"origin": origin or abs_path, "trust": trust,
                      "site_direct": site,
                      "external": {"site": site, "home": abs_path,
                                   "bytes": total,
                                   "files": len(ingest_rows)}})
            self.store.set_location(ref, site, f"external:{abs_path}")
            self.store.emit("data.external_registered", ref=ref,
                            site=site, home=abs_path, bytes=total,
                            files=len(ingest_rows))
            return {"ref": ref, "kind": "tree", "bytes": total,
                    "files": len(ingest_rows), "external_home": abs_path,
                    "trust": trust,
                    "note": "referenced in place — no copy; same-site "
                            "tasks stage a symlink behind a stat-fence; "
                            "blobs ingest lazily if they ever must move"}
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

    # -- reference-in-place (external homes) --------------------------------

    def external_home_at(self, ref: str, site: str) -> str | None:
        """The durable-home path of an external-source ref at `site`,
        None when the ref is (or has become) CAS-backed there."""
        for l in self.store.locations_of(ref):
            if l["site"] == site and str(l["path"]).startswith("external:"):
                return str(l["path"])[len("external:"):]
        return None

    def _fence_external(self, adapter, ref: str, home: str) -> None:
        """Stat-fence at staging: the home must still LOOK like what was
        registered (size/mtime for a file; file count + total bytes for
        a tree). Cheap by design — content drift subtler than a stat
        change is caught by hash verification whenever bytes move. A
        drifted home means the ref is DEAD (identity is content):
        re-registering mints the new content's ref."""
        import shlex as _sh
        row = self.store.get_dataref(ref) or {}
        ext = (row.get("meta") or {}).get("external") or {}

        def _fail(observed):
            raise WeftError(
                "data.verify_failed",
                f"external source for {ref} changed or vanished",
                stage="staging",
                hints={"source": "external-home", "site": adapter.name,
                       "home": home,
                       "recorded": {k: v for k, v in ext.items()
                                    if k in ("bytes", "mtime", "files")},
                       "observed": observed,
                       "suggestion": "the durable home moved or its "
                                     "content changed: restore it, or "
                                     "re-register the path (new content "
                                     "= new ref) and update the task"})
        if row.get("kind") == "tree":
            r = adapter.shim(["list-tree", "--root", home,
                              "--max", "100000"], timeout=900)
            if r.rc != 0:
                _fail({"error": (r.err or r.out)[:200]})
            files, nbytes = 0, 0
            for line in r.out.splitlines():
                p = line.split("\t")
                if len(p) >= 2:
                    files += 1
                    nbytes += int(p[1])
            if files != ext.get("files") or nbytes != ext.get("bytes"):
                _fail({"files": files, "bytes": nbytes})
            return
        q = _sh.quote(home)
        r = adapter.run_cmd(
            f"[ -f {q} ] && printf '%s %s' "
            f"\"$(wc -c < {q} | tr -d ' ')\" "
            f"\"$(stat -c %Y {q} 2>/dev/null || stat -f %m {q})\"",
            timeout=300)
        parts = (r.out or "").split()
        if r.rc != 0 or len(parts) < 2:
            _fail({"error": "missing or unreadable"})
        if int(parts[0]) != ext.get("bytes") \
                or int(parts[1]) != ext.get("mtime"):
            _fail({"bytes": int(parts[0]), "mtime": int(parts[1])})

    def _ingest_external(self, adapter, ref: str, home: str) -> None:
        """Bytes must MOVE (cross-site staging / fetch): ingest the
        external home into ITS OWN site's CAS first, so every transfer
        path sees a normal blob layout. Fence first — never ingest
        drifted content under an old identity; wire verification then
        re-checks content hashes as bytes move."""
        import shlex as _sh
        import uuid as _uuid
        self._fence_external(adapter, ref, home)
        row = self.store.get_dataref(ref) or {}
        endpoint = adapter.transfer_endpoint()
        if row.get("kind") == "tree":
            manifest = self.cas.tree_manifest(ref)
            rows = [f"{e['path']}\t{e['sha256']}" for e in manifest
                    if e["kind"] == "file"]
            plan_rel = f"tmp/ingest-{_uuid.uuid4().hex[:10]}.tsv"
            adapter.write_file(plan_rel, ("\n".join(rows) + "\n").encode())
            r = adapter.shim(
                ["ingest", "--cas", endpoint["cas_root"], "--root", home,
                 "--plan", adapter.path(plan_rel)], timeout=3600)
            adapter.run_cmd(
                f"rm -f {_sh.quote(adapter.path(plan_rel))}", timeout=120)
            if r.rc != 0:
                raise WeftError("data.transfer_failed",
                                f"ingest of external tree failed: "
                                f"{(r.err or r.out)[:200]}",
                                stage="staging", retryable=True)
            for e in manifest:
                if e["kind"] != "file":
                    continue
                fref = f"dref:{e['sha256']}"
                self.store.put_dataref(fref, "file", e["size"],
                                       meta={"origin": home,
                                             "exec": e.get("exec", False)})
                self.store.set_location(fref, adapter.name,
                                        endpoint["cas_root"])
        else:
            digest = ref.split(":")[-1]
            blob_dir = f"{endpoint['cas_root']}/{digest[:2]}"
            blob = f"{blob_dir}/{digest}"
            r = adapter.run_cmd(
                f"mkdir -p {_sh.quote(blob_dir)} && "
                f"([ -f {_sh.quote(blob)} ] || ln {_sh.quote(home)} "
                f"{_sh.quote(blob)} 2>/dev/null || cp -p {_sh.quote(home)} "
                f"{_sh.quote(blob)})", timeout=3600)
            if r.rc != 0:
                raise WeftError("data.transfer_failed",
                                f"ingest of external file failed: "
                                f"{r.err[:200]}",
                                stage="staging", retryable=True)
        # the location flips to CAS-backed; meta.external stays as record
        self.store.set_location(ref, adapter.name, endpoint["cas_root"])
        self.store.emit("data.ingested_for_transfer", ref=ref,
                        site=adapter.name, home=home,
                        bytes=row.get("bytes", 0))

    def ensure_from_keep(self, ref: str, adapters: dict) -> bool:
        """retention2.md LINK, the consuming half: a ref whose caches
        were evicted but whose bytes were RETAINED re-enters from the
        keep — hash-verified against the known digest (drift = honest
        data.verify_failed, never silent). Returns True when the ref
        gained a live location."""
        import shlex as _sh
        row = self.store.get_dataref(ref)
        keep = ((row or {}).get("meta") or {}).get("keep")
        if not keep:
            return False
        digest = ref.split(":")[-1]
        if keep["site"] == "@workspace":
            p = Path(keep["path"])
            if not p.is_file():
                return False
            info = self.cas.register_file(p)
            if info.ref != ref:
                raise WeftError(
                    "data.verify_failed",
                    f"retained copy of {ref} no longer matches its "
                    f"content", stage="staging",
                    hints={"source": "keep", "path": keep["path"],
                           "suggestion": "the keep was modified; "
                                         "re-register it as new content"})
            self.store.set_location(ref, "@workspace", str(self.cas.root))
            return True
        adapter = adapters.get(keep["site"])
        if adapter is None:
            return False
        endpoint = adapter.transfer_endpoint()
        blob_dir = f"{endpoint['cas_root']}/{digest[:2]}"
        q = _sh.quote(keep["path"])
        r = adapter.run_cmd(
            f"[ -f {q} ] || exit 9; "
            f"h=$(sha256sum {q} 2>/dev/null || shasum -a 256 {q}); "
            f"[ \"${{h%% *}}\" = {digest} ] || exit 8; "
            f"mkdir -p {_sh.quote(blob_dir)} && "
            f"([ -f {_sh.quote(blob_dir + '/' + digest)} ] || "
            f"ln {q} {_sh.quote(blob_dir + '/' + digest)} 2>/dev/null || "
            f"cp -p {q} {_sh.quote(blob_dir + '/' + digest)})",
            timeout=3600)
        if r.rc == 9:
            return False                       # keep gone (forgotten?)
        if r.rc == 8:
            raise WeftError(
                "data.verify_failed",
                f"retained copy of {ref} no longer matches its content",
                stage="staging",
                hints={"source": "keep", "site": keep["site"],
                       "path": keep["path"],
                       "suggestion": "the keep was modified; re-register "
                                     "it as new content"})
        if r.rc != 0:
            return False
        self.store.set_location(ref, keep["site"], endpoint["cas_root"])
        self.store.emit("data.reobtained_from_keep", ref=ref,
                        site=keep["site"], target=keep.get("target"))
        return True

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
            # `external` is a TYPED fact (a reference-in-place home) —
            # consumers were about to parse the "external:" path prefix
            # themselves, the string-grammar-two-implementations trap
            {"site": loc["site"], "verified_at": loc["verified_at"],
             "external": str(loc["path"]).startswith("external:")}
            for loc in self.store.locations_of(ref)
        ]
        return row

    def members(self, ref: str, limit: int = 500,
                cursor: int | None = None) -> dict:
        """Tree member enumeration in MANIFEST ORDER (the order bytes
        were hashed — streamed stores prefetch by it), from the local
        manifest, which exists for BOTH lanes (ingested trees write it
        at registration; site-side/ingest=False trees adopt the
        site-computed manifest the same moment). Cursor = plain index
        into the immutable manifest: content-addressed, so a page can
        never shift under a reader."""
        row = self.store.get_dataref(ref)
        if not row:
            raise WeftError("data.missing", f"unknown ref: {ref}",
                            stage="staging")
        if row.get("kind") != "tree":
            raise WeftError(
                "task.invalid",
                f"{ref} is a {row.get('kind')}, not a tree — only trees "
                "have members",
                stage="infra",
                hints={"suggestion": "data_describe/data_stat cover "
                                     "single files"})
        try:
            manifest = self.cas.tree_manifest(ref)
        except WeftError:
            raise WeftError(
                "data.missing",
                f"no manifest for {ref} in this workspace — cannot "
                "enumerate members",
                stage="staging",
                hints={"suggestion": "re-register the tree (site-side "
                                     "registration adopts the manifest "
                                     "locally)"})
        start = int(cursor or 0)
        limit = max(1, min(int(limit), 10_000))
        page = manifest[start:start + limit]
        out = {
            "ref": ref,
            "total": len(manifest),
            "members": [{"path": e["path"], "bytes": e.get("bytes"),
                         "sha256": e.get("sha256"),
                         **({"kind": e["kind"]}
                            if e.get("kind") not in (None, "file") else {})}
                        for e in page],
        }
        if start + limit < len(manifest):
            out["next_cursor"] = start + limit
        return out

    # -- targeted eviction (footprint round 26) -----------------------------

    def _copies_of(self, ref: str, hexdigest: str) -> list[dict]:
        """Every place the record says these bytes live: location rows
        (present=1; external homes count — they ARE the durable
        original), the workspace CAS blob, and a keep anchor (LINK
        re-obtains from keeps hash-verified). RECORD-based, like gc —
        the verify fence turns a lying record into a loud re-transfer,
        and the UI's preflight live-verifies via data_stat anyway."""
        copies = [{"at": l["site"],
                   "external": str(l["path"]).startswith("external:")}
                  for l in self.store.locations_of(ref)
                  if l["site"] != "@workspace"]
        if self.cas._blob_path(hexdigest).exists():
            copies.append({"at": "@workspace"})
        keep = ((self.store.get_dataref(ref) or {}).get("meta")
                or {}).get("keep")
        if keep:
            copies.append({"at": f"keep:{keep['target']}",
                           "keep": True})
        return copies

    def _evict_plan(self, ref: str, at: str, force: bool,
                    pinned: set) -> dict:
        """ONE evaluator for dry_run AND execution — preview and real
        cannot drift. Returns {units: [(kind, ref, hex, bytes)],
        kept: [...], refusal: WeftError|None, would_free_bytes,
        remaining: [...]}. kind: "blob-ws" | "location"."""
        row = self.store.get_dataref(ref)
        if not row:
            raise WeftError("data.missing", f"unknown ref: {ref}",
                            stage="staging")
        loc_here = next((l for l in self.store.locations_of(ref)
                         if l["site"] == at), None)
        if at != "@workspace":
            if self.adapters_hint and at not in self.adapters_hint:
                raise WeftError("task.invalid", f"unknown site: {at}",
                                stage="staging",
                                hints={"registered":
                                       sorted(self.adapters_hint)})
            if loc_here is None:
                raise WeftError("data.missing",
                                f"{ref} has no recorded copy at {at}",
                                stage="staging")
            if str(loc_here["path"]).startswith("external:"):
                # NOT force-able: this is the owner's original, not a
                # weft copy at any force level
                raise WeftError(
                    "data.external_home",
                    f"the copy at {at} is a reference-in-place home — "
                    f"not weft's to delete",
                    stage="staging",
                    hints={"home": str(loc_here["path"])
                           [len("external:"):]})
        members: list[tuple] = []      # (member_ref, hex, bytes)
        if row["kind"] == "tree":
            for e in self.cas.tree_manifest(ref):
                if e.get("kind") == "file":
                    members.append((f"dref:{e['sha256']}", e["sha256"],
                                    e.get("size") or 0))
        else:
            members.append((ref, ref[len("dref:"):], row["bytes"] or 0))
        units, kept = [], []
        refusal = None
        if at == "@workspace" and ref in pinned and not force:
            refusal = WeftError(
                "data.pinned",
                f"{ref} is provenance-reachable — the workspace record "
                f"must survive (gc's rule; force=True overrides "
                f"loudly)", stage="staging",
                hints={"copies": self._copies_of(
                    ref, members[0][1] if members else "")})
        remaining_all: set = set()
        for mref, hexd, nbytes in members:
            copies = self._copies_of(mref, hexd)
            others = [c for c in copies if c["at"] != at]
            # for tree members, the TREE's own copies elsewhere count
            # (an external home or another site holds the whole tree)
            if row["kind"] == "tree" and mref != ref:
                others += [c for c in self._copies_of(
                    ref, ref[len("dref:"):]) if c["at"] != at]
            here = ((at == "@workspace"
                     and self.cas._blob_path(hexd).exists())
                    or (at != "@workspace"
                        and any(c["at"] == at for c in copies)))
            if not here:
                continue                    # nothing of this member at `at`
            if not others and not force:
                kept.append({"ref": mref, "why": "last_copy",
                             "bytes": nbytes})
                continue
            units.append((mref, hexd, nbytes))
            remaining_all.update(c["at"] for c in others)
        if not units and kept and refusal is None:
            refusal = WeftError(
                "data.last_copy",
                f"every byte of {ref} at {at} is the last live copy "
                f"the record knows — force=True destroys it",
                stage="staging",
                hints={"copies_checked": ["locations", "workspace CAS",
                                          "keep anchors"],
                       "kept": kept})
        return {"units": units, "kept": kept, "refusal": refusal,
                "would_free_bytes": sum(u[2] for u in units),
                "remaining": sorted(remaining_all)}

    def evict(self, ref: str, at: str, adapters: dict,
              pinned: set, dry_run: bool = False,
              force: bool = False) -> dict:
        """Drop ONE copy of a dataset at a named place — the targeted
        reclaim verb (every other tier has one). check -> demote ->
        delete: a crash between demote and delete leaves orphans for
        gc_orphans, never a lost last copy."""
        self.adapters_hint = set(adapters)      # for _evict_plan
        plan = self._evict_plan(ref, at, force, pinned)
        receipt = {"ref": ref, "at": at,
                   "would_free_bytes" if dry_run else "bytes_freed":
                       plan["would_free_bytes"],
                   "remaining": plan["remaining"]}
        if plan["kept"]:
            receipt["kept"] = plan["kept"]
        if len(plan["units"]) > (0 if plan["refusal"] else 1)                 or (self.store.get_dataref(ref) or {}).get(
                    "kind") == "tree":
            receipt["evicted_members"] = [u[0] for u in plan["units"]]
        if plan["refusal"] is not None:
            if dry_run:
                receipt["refusal"] = plan["refusal"].to_dict()
                receipt["would_free_bytes"] = 0
                return receipt
            raise plan["refusal"]
        if dry_run:
            return receipt
        # execute the SAME plan: demote rows first, delete bytes after
        if at == "@workspace":
            for mref, hexd, _ in plan["units"]:
                self.store.demote_location(mref, "@workspace")
                self.store.demote_location(ref, "@workspace")
                bp = self.cas._blob_path(hexd)
                try:
                    bp.unlink()
                except OSError:
                    pass
        else:
            adapter = adapters[at]
            # capture the CAS root BEFORE demoting (demoted rows leave
            # locations_of), then demote, then delete bytes
            loc = next((l for l in self.store.locations_of(ref)
                        if l["site"] == at), None)
            cas_root = str(loc["path"]) if loc else None
            for mref, _, _ in plan["units"]:
                self.store.demote_location(mref, at)
            if not plan["kept"]:
                self.store.demote_location(ref, at)
            paths = []
            for _, hexd, _ in plan["units"]:
                if cas_root and not cas_root.startswith("external:"):
                    paths.append(f"{cas_root}/{hexd[:2]}/{hexd}")
            if paths:
                adapter.run_cmd(
                    "rm -f " + " ".join(shlex.quote(p) for p in paths),
                    timeout=300)
        self.store.emit("data.evicted", ref=ref, at=at,
                        bytes=plan["would_free_bytes"],
                        forced=bool(force))
        return receipt

    # -- observation (the ref-addressed tier: fileio engine) ----------------

    def _ref_target(self, ref: str, rel: str | None) -> tuple[dict, str, int | None]:
        """(dataref row, target blob hex, recorded size) for a ref or a
        tree member. Tree refs REQUIRE rel= (a member path); file refs
        refuse it — loudly, both ways."""
        row = self.store.get_dataref(ref)
        if not row:
            raise WeftError("data.missing", f"unknown ref: {ref}",
                            stage="staging")
        if row["kind"] == "tree":
            if not rel:
                raise WeftError(
                    "task.invalid",
                    "tree refs need rel= — a member path inside the "
                    "tree", stage="staging",
                    hints={"kind": "tree", "bytes": row["bytes"]})
            entry = next((e for e in self.cas.tree_manifest(ref)
                          if e["path"] == rel), None)
            if entry is None:
                raise WeftError(
                    "data.missing",
                    f"no such member in the tree: {rel!r}",
                    stage="staging")
            if entry["kind"] != "file":
                raise WeftError(
                    "task.invalid",
                    f"tree member {rel!r} is a {entry['kind']}, not a "
                    f"file", stage="staging")
            return row, entry["sha256"], entry.get("size")
        if rel:
            raise WeftError(
                "task.invalid",
                "rel= addresses a member of a TREE ref; this ref is a "
                f"{row['kind']}", stage="staging")
        return row, ref[len("dref:"):], row["bytes"]

    def ref_candidates(self, ref: str, rel: str | None, adapters: dict,
                       site: str | None = None) -> list[dict]:
        """fileio candidates for a ref's bytes (or a tree member's), in
        preference order: the workspace CAS copy first (bytes are
        content-addressed — identical everywhere, and a local pread
        beats any transport), then each recorded site location
        (reference-in-place home, or the site CAS blob). site= narrows
        the REMOTE candidates only."""
        row, hexdigest, _ = self._ref_target(ref, rel)
        cands: list[dict] = []
        bp = self.cas._blob_path(hexdigest)
        if bp.exists():
            cands.append({"at": "workspace", "adapter": None,
                          "path": str(bp), "root": str(self.cas.root)})
        locs = list(self.store.locations_of(ref))
        if row["kind"] == "tree":
            locs += self.store.locations_of(f"dref:{hexdigest}")
        seen: set[tuple] = set()
        for loc in locs:
            if site and loc["site"] != site:
                continue
            adapter = adapters.get(loc["site"])
            if adapter is None:
                continue                # unregistered site: not reachable
            lpath = str(loc["path"])
            if lpath.startswith("external:"):
                home = lpath[len("external:"):]
                if row["kind"] == "tree":
                    joined = os.path.normpath(os.path.join(home, rel))
                    if not joined.startswith(
                            os.path.normpath(home) + "/"):
                        raise WeftError(
                            "task.invalid",
                            f"member path escapes the external home: "
                            f"{rel!r}", stage="staging")
                    cand = {"at": loc["site"], "via": "external-home",
                            "adapter": adapter, "path": joined,
                            "root": os.path.normpath(home)}
                else:
                    cand = {"at": loc["site"], "via": "external-home",
                            "adapter": adapter, "path": home,
                            "root": os.path.dirname(home.rstrip("/"))}
            else:
                cand = {"at": loc["site"], "via": "site-cas",
                        "adapter": adapter,
                        "path": f"{lpath}/{hexdigest[:2]}/{hexdigest}",
                        "root": lpath}
            key = (cand["at"], cand["path"])
            if key not in seen:
                seen.add(key)
                cands.append(cand)
        return cands

    def read_range(self, ref: str, adapters: dict, rel: str | None = None,
                   offset: int = 0, length: int | None = None,
                   site: str | None = None) -> dict:
        """The ref-addressed range tier — same engine, same semantics as
        run_file_read_range (one transport engine, two addressings).
        NOTE: a range slice is unverifiable in ANY scheme (you cannot
        check a sha256 from a slice) — a viewing tier; computation
        inputs keep going through whole-content verified fetch."""
        from .fileio import range_read
        got = range_read(
            self.ref_candidates(ref, rel, adapters, site=site),
            offset, length,
            what=f"{ref}" + (f"/{rel}" if rel else ""),
            missing_hints={"note": "no live copy at the workspace or "
                                   "any registered location; data_stat "
                                   "shows where the record thinks the "
                                   "bytes are"})
        out = {"ref": ref, **got}
        if rel:
            out["rel"] = rel
        return out

    def read_range_many(self, ref: str, adapters: dict, rels: list,
                        site: str | None = None) -> dict:
        """Batched WHOLE-member reads of a TREE ref — the chunk-cascade
        shape (aba rates note): one remote invocation per location
        group instead of N round-trip floors."""
        from .fileio import range_read_many
        if not isinstance(rels, list) or not rels or \
                not all(isinstance(r, str) and r for r in rels):
            raise WeftError("task.invalid",
                            "rels must be a non-empty list of member "
                            "paths", stage="staging")
        if len(rels) > 500:
            raise WeftError("task.invalid",
                            f"{len(rels)} rels in one call (cap 500) — "
                            f"chunk the request", stage="staging")
        row = self.store.get_dataref(ref)
        if not row:
            raise WeftError("data.missing", f"unknown ref: {ref}",
                            stage="staging")
        if row["kind"] != "tree":
            raise WeftError("task.invalid",
                            "rels=[...] batches members of a TREE ref",
                            stage="staging", hints={"kind": row["kind"]})
        cands = {}
        for rel in dict.fromkeys(rels):
            try:
                cands[rel] = self.ref_candidates(ref, rel, adapters,
                                                 site=site)
            except WeftError as e:
                # a per-member refusal (absent member, link, escape)
                # rides the batch verbatim as that entry's answer
                cands[rel] = e
        pre_errors = {r: c.to_dict() for r, c in cands.items()
                      if isinstance(c, WeftError)}
        good = {r: c for r, c in cands.items()
                if not isinstance(c, WeftError)}
        got = range_read_many(
            good,
            missing_hints={"note": "data_stat shows where the record "
                                   "thinks the bytes are"})
        got["files"].update(pre_errors)
        return {"ref": ref, **got}

    def stat_ref(self, ref: str, adapters: dict,
                 site: str | None = None, sample: int = 20,
                 probe_results: dict | None = None,
                 collect_into: dict | None = None) -> dict:
        """Live observation vs the record: where does this ref's data
        actually sit RIGHT NOW. Non-mutating — divergence is reported,
        never acted on (staging's verify fence stays the sole authority
        that demotes locations; doctor/reconcile mutate). Trees are
        observed by SAMPLE (first `sample` members, said honestly) —
        an exact tree audit is data_fingerprint's job."""
        from .fileio import stat_batch, stat_candidate
        row = self.store.get_dataref(ref)
        if not row:
            raise WeftError("data.missing", f"unknown ref: {ref}",
                            stage="staging")
        kind = row["kind"]
        recorded = {"kind": kind, "bytes": row["bytes"],
                    "trust": (row["meta"] or {}).get("trust")}
        members: list[dict] = []
        if kind == "tree":
            members = [e for e in self.cas.tree_manifest(ref)
                       if e["kind"] == "file"]
            recorded["files"] = len(members)
            probe = members[:max(0, sample)]
        # workspace observation (local Path checks — cheap)
        if kind == "tree":
            present = sum(
                1 for e in probe
                if self.cas._blob_path(e["sha256"]).exists())
            workspace = {"present": present == len(probe) and bool(probe),
                         "members_checked": len(probe),
                         "members_present": present,
                         "sampled": len(probe) < len(members)}
        else:
            st = stat_candidate({"adapter": None,
                                 "path": str(self.cas._blob_path(
                                     ref[len("dref:"):]))})
            workspace = ({"present": True, **st} if st
                         else {"present": False})
        sites = []
        # divergence = a RECORDED location observes absent, or no live
        # copy is observable anywhere. Workspace absence alone is NOT
        # divergence — the record never claims workspace presence (a
        # reference-in-place ref legitimately has no local copy).
        site_divergent = False
        any_live = workspace.get("present") is True
        any_unknown = False
        for loc in self.store.locations_of(ref):
            if loc["site"] == "@workspace":
                continue        # the workspace block above IS this row
            if site and loc["site"] != site:
                continue
            adapter = adapters.get(loc["site"])
            entry: dict = {"site": loc["site"],
                           "verified_at": loc["verified_at"]}
            if adapter is None:
                entry["present"] = "unknown"
                entry["reason"] = "site not registered in this workspace"
                any_unknown = True
                sites.append(entry)
                continue
            lpath = str(loc["path"])
            external = lpath.startswith("external:")
            entry["via"] = "external-home" if external else "site-cas"
            if kind == "tree":
                if external:
                    home = lpath[len("external:"):]
                    paths = [os.path.normpath(os.path.join(home,
                                                           e["path"]))
                             for e in probe]
                else:
                    paths = [f"{lpath}/{e['sha256'][:2]}/{e['sha256']}"
                             for e in probe]
                if collect_into is not None:
                    collect_into.setdefault(loc["site"],
                                            set()).update(paths)
                    got = {p: None for p in paths}     # dry pass
                elif probe_results is not None:
                    got = {p: probe_results.get((loc["site"], p))
                           for p in paths}
                else:
                    got = stat_batch(adapter, paths)
                present = sum(1 for v in got.values() if v is not None)
                entry.update(members_checked=len(probe),
                             members_present=present,
                             present=present == len(probe)
                             and bool(probe),
                             sampled=len(probe) < len(members))
            else:
                hexd = ref[len("dref:"):]
                path = (lpath[len("external:"):] if external
                        else f"{lpath}/{hexd[:2]}/{hexd}")
                if collect_into is not None:
                    collect_into.setdefault(loc["site"],
                                            set()).add(path)
                    st = None                          # dry pass
                elif probe_results is not None:
                    st = probe_results.get((loc["site"], path))
                else:
                    st = stat_candidate({"adapter": adapter,
                                         "path": path})
                entry.update({"present": True, **st} if st
                             else {"present": False})
            if entry.get("present") is False:
                site_divergent = True
            elif entry.get("present") is True:
                any_live = True
            sites.append(entry)
        divergent = site_divergent or (not any_live and not any_unknown)
        return {"ref": ref, "recorded": recorded, "workspace": workspace,
                "sites": sites, "divergent": divergent,
                "note": "observation only — nothing was demoted; "
                        "staging's verify fence acts on divergence "
                        "(doctor/reconcile mutate)"}

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
        # external-home refs used ON their own site: stat-fence, no bytes
        for ref in dict.fromkeys(refs):
            home = self.external_home_at(ref, adapter.name)
            if home:
                self._fence_external(adapter, ref, home)
        plan = self.plan_for(refs, adapter.name)
        if plan.to_transfer and adapters:
            # refs with NO live source but a retained keep re-enter from
            # the keep first (retention2 LINK — hash-verified); a keep on
            # the TARGET site satisfies the plan outright on re-plan
            for ref in list(plan.to_transfer):
                if not self.store.locations_of(ref) \
                        and self.cas.kind_of(ref) is None:
                    self.ensure_from_keep(ref, adapters)
            plan = self.plan_for(refs, adapter.name)
        if plan.to_transfer and adapters:
            # bytes must MOVE: external sources ingest into their own
            # site's CAS first (fence + wire hashes keep it honest), then
            # every existing route works unchanged
            for ref in plan.to_transfer:
                for l in self.store.locations_of(ref):
                    if str(l["path"]).startswith("external:") \
                            and l["site"] in adapters:
                        self._ingest_external(
                            adapters[l["site"]], ref,
                            str(l["path"])[len("external:"):])
                        break
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

    def materialize_plan(self, task: Task, site: str | None = None) -> str:
        """TSV consumed by `weft-shim materialize` (path, sha/target, flag).
        With `site`, refs whose bytes live at an external home ON that
        site mount as one SYMLINK to the home (file or tree root) — the
        zero-copy path for reference-in-place data; the read-only inputs
        contract is what makes it safe (as with hardlink staging)."""
        rows: list[str] = []
        mounts = list(task.inputs) + ([task.code] if task.code else [])
        for inp in mounts:
            if site:
                home = self.external_home_at(inp.ref, site)
                if home:
                    rows.append(f"{inp.mount_as}\t{home}\tL")
                    continue
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
                _require_digest(digest, out, adapter.name)
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
                kind, path, is_exec, size, digest = _tsv_row(
                    line, f"collection of {out!r}")
                if kind == "link":
                    tree_entries.append({"path": path, "kind": "link", "target": digest})
                    continue
                _require_digest(digest, f"{out}/{path}", adapter.name)
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
                # a failed hardlink/copy into the site CAS is a TRANSFER
                # problem — verify_failed ("corrupt content") sent agents
                # re-running healthy jobs (2026-07 sweep #20)
                raise WeftError(
                    "data.transfer_failed",
                    f"output ingest failed: {r.err[:300]}",
                    stage="collecting", retryable=True,
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
                if str(remote[0]["path"]).startswith("external:"):
                    # bytes must move: ingest the home into its own
                    # site's CAS first (fenced + wire-verified)
                    self._ingest_external(
                        adapter, ref,
                        str(remote[0]["path"])[len("external:"):])
                endpoint = adapter.transfer_endpoint()
                method = transfers.get(endpoint["method"])
                method.fetch(list(missing), self.cas, endpoint)
        elif kind_here is None:
            # find a site that has it and pull blobs back
            locations = self.store.locations_of(ref)
            remote = [l for l in locations if l["site"] in adapters]
            if not remote and self.ensure_from_keep(ref, adapters):
                # caches were evicted but the bytes were RETAINED: the
                # keep re-entered (site CAS, or the LOCAL cas for a
                # home keep) and the fetch proceeds normally
                locations = self.store.locations_of(ref)
                remote = [l for l in locations if l["site"] in adapters]
            if not remote and self.cas.kind_of(ref) is None:
                raise WeftError(
                    "data.missing",
                    f"no known location holds {ref}", stage="staging",
                    hints={"note": "caches evicted and no retained "
                                   "keep carries this ref"})
            if remote:
                loc = remote[0]
                adapter = adapters[loc["site"]]
                if str(loc["path"]).startswith("external:"):
                    self._ingest_external(
                        adapter, ref, str(loc["path"])[len("external:"):])
                endpoint = adapter.transfer_endpoint()
                method = transfers.get(endpoint["method"])
                row = self.store.get_dataref(ref)
                if row and row["kind"] == "tree":
                    raise WeftError(
                        "data.missing",
                        "this workspace has no manifest for the tree — it "
                        "can enumerate no blobs to pull",
                        stage="staging",
                        hints={"suggestion": "register the tree "
                                             "(data_register(path, "
                                             "site=...)) or import the "
                                             "bundle that minted it first"},
                    )
                method.fetch(
                    [(ref.split(":")[-1], row["bytes"] if row else 0)],
                    self.cas, endpoint)
        # verify + materialize into workspace
        if not self.cas.verify(ref):
            raise WeftError("data.verify_failed", f"content of {ref} failed verification",
                            stage="staging")
        self.cas.materialize(ref, dest, mode="copy")
        return {"ref": ref, "path": str(dest)}
