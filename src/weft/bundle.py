"""Reproducibility bundles: one file that re-derives a result anywhere.

`bundle_export(job_id)` walks the provenance closure of a finished job —
the task, its environment identities (specs + canonical locks + native
lockfiles), every input blob (and the jobs that produced them,
recursively), and the recorded output refs — into a single tarball with a
manifest. `bundle_import` loads it into any workspace; re-running the
target task there must reproduce the recorded output refs (that assertion
IS the acceptance test, and the honest limit of the claim: an
`escape-hatch` env re-executes its captured installers; `attested`
modules must exist on the destination site).
"""

from __future__ import annotations

import json
import tarfile
import tempfile
import time
from pathlib import Path

from .errors import WeftError

SCHEMA = "bundle:v1"

# the host envelope is a sealed carrier, not a data transport — data
# belongs in blobs, where it is content-addressed and verified
_METADATA_CAP = 64 << 20


def _closure(weft, job_id: str, depth: int = 10):
    """jobs, envs, refs reachable from the target through inputs."""
    jobs: dict[str, dict] = {}
    envs: set[str] = set()
    refs: set[str] = set()
    frontier = [(job_id, depth)]
    while frontier:
        jid, d = frontier.pop()
        if jid in jobs:
            continue
        job = weft.store.get_job(jid)
        if not job:
            raise WeftError("task.invalid", f"unknown job: {jid}",
                            stage="infra")
        if job["state"] != "DONE":
            raise WeftError(
                "task.invalid",
                f"job {jid} is {job['state']} — bundles capture FINISHED "
                "results", stage="infra")
        jobs[jid] = job
        task = job["task"]
        if task.get("env"):
            envs.add(task["env"])
        ins = [i["ref"] for i in task.get("inputs") or []]
        if task.get("code"):
            ins.append(task["code"]["ref"])
        for ref in ins:
            refs.add(ref)
            row = weft.store.get_dataref(ref)
            origin = ((row or {}).get("meta") or {}).get("origin", "")
            if origin.startswith("job:jobs/") and d > 0:
                frontier.append((origin.split("/", 1)[1], d - 1))
        for o in (job["manifest"] or {}).get("outputs", []):
            refs.add(o["ref"])
    return jobs, envs, refs


def export_bundle(weft, job_id: str, out_path: str,
                  metadata=None) -> dict:
    """`metadata` is a caller-owned sealed envelope: bytes or any JSON
    value, stored as a separate archive member and returned verbatim by
    import. weft never parses it, and it does not enter the bundle's
    identity or the re-derivation proof. It belongs to THIS export call —
    re-exporting an imported bundle does not carry an old envelope
    forward (no inheritance, so no merge semantics to invent)."""
    meta_name = meta_bytes = None
    if metadata is not None:
        if isinstance(metadata, (bytes, bytearray)):
            meta_name, meta_bytes = "host-metadata.bin", bytes(metadata)
        else:
            try:
                meta_bytes = json.dumps(metadata).encode()
            except (TypeError, ValueError) as e:
                raise WeftError(
                    "task.invalid",
                    f"bundle metadata must be bytes or JSON-serializable: {e}",
                    stage="staging")
            meta_name = "host-metadata.json"
        if len(meta_bytes) > _METADATA_CAP:
            raise WeftError(
                "task.invalid",
                f"bundle metadata is {len(meta_bytes)} bytes (cap "
                f"{_METADATA_CAP}) — the envelope carries context, not "
                "data; register large content as a DataRef instead",
                stage="staging")
    jobs, env_ids, refs = _closure(weft, job_id)

    envs = {}
    for eid in env_ids:
        row = weft.store.get_env(eid)
        if not row:
            raise WeftError("task.invalid", f"unknown EnvID: {eid}",
                            stage="infra")
        envs[eid] = {
            **row,
            "spec_body": weft.store.get_spec(row["spec_hash"]),
        }
        # a captured installer input is part of the env's identity: its
        # blob must travel or the escape hatch stops being portable
        for inp in row["canonical"]["extras"].get("post_install_inputs") or []:
            if inp.get("ref"):
                refs.add(inp["ref"])

    missing, blobs, trees = [], {}, {}
    queue = sorted(refs)
    seen = set()
    while queue:
        ref = queue.pop()
        if ref in seen:
            continue
        seen.add(ref)
        digest = ref.split(":")[-1]
        kind = weft.cas.kind_of(ref)
        if kind is None:
            # not in the workspace CAS (e.g. produced and left site-side):
            # pull it home first — a bundle with holes reproduces nothing
            try:
                with tempfile.TemporaryDirectory() as td:
                    weft.data_fetch(ref, f"{td}/blob")
                kind = weft.cas.kind_of(ref)
                assert kind is not None
            except (WeftError, AssertionError):
                missing.append(ref)
                continue
        if kind == "tree":
            tree_path = weft.cas.root / "trees" / f"{digest}.json"
            trees[digest] = tree_path
            for entry in json.loads(tree_path.read_text()):
                if entry.get("kind") == "file":
                    queue.append(f"dref:{entry['sha256']}")
            continue
        row = weft.store.get_dataref(ref)
        chunk_digests = (row or {}).get("chunks") or [digest]
        for cd in chunk_digests:
            p = weft.cas._blob_path(cd)
            if p.exists():
                blobs[cd] = p
            else:
                missing.append(ref)
    if missing:
        raise WeftError(
            "data.missing",
            f"{len(missing)} ref(s) unavailable from any site",
            stage="staging",
            hints={"missing": missing[:10],
                   "suggestion": "the content was evicted everywhere; "
                                 "re-run the producing job first"})

    manifest = {
        "schema": SCHEMA,
        "target_job": job_id,
        "created_at": time.time(),
        "reproducibility": (jobs[job_id]["manifest"] or {}).get(
            "reproducibility", "unknown"),
        "jobs": {j: {"task": row["task"], "task_hash": row["task_hash"],
                     "manifest": row["manifest"], "site": row["site"]}
                 for j, row in jobs.items()},
        "envs": {e: {"spec_hash": row["spec_hash"],
                     "spec_body": row["spec_body"],
                     "canonical": row["canonical"],
                     "native_lock": row["native_lock"],
                     "manifest": row["manifest"],
                     "platforms": row["platforms"],
                     "weakly_reproducible": row["weakly_reproducible"],
                     "parent_env_id": row.get("parent_env_id"),
                     "layerable": row.get("layerable")}
                 for e, row in envs.items()},
        "datarefs": {r: weft.store.get_dataref(r) for r in sorted(refs)},
    }

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        mj = json.dumps(manifest, indent=1).encode()
        info = tarfile.TarInfo("bundle/manifest.json")
        info.size = len(mj)
        import io
        tar.addfile(info, io.BytesIO(mj))
        if meta_name:
            info = tarfile.TarInfo(f"bundle/{meta_name}")
            info.size = len(meta_bytes)
            tar.addfile(info, io.BytesIO(meta_bytes))
        for digest, path in blobs.items():
            tar.add(str(path), arcname=f"bundle/blobs/{digest}")
        for digest, path in trees.items():
            tar.add(str(path), arcname=f"bundle/trees/{digest}.json")

    weft.store.audit_log(None, "bundle.export", command=job_id,
                         result=str(out))
    weft.store.emit("bundle.exported", job_id=job_id, path=str(out),
                    jobs=len(jobs), envs=len(envs), blobs=len(blobs))
    return {"path": str(out), "bytes": out.stat().st_size,
            "target_job": job_id, "jobs": len(jobs), "envs": len(envs),
            "blobs": len(blobs),
            **({"metadata_bytes": len(meta_bytes)} if meta_name else {}),
            "reproducibility": manifest["reproducibility"],
            "note": "bundle_import(path) into ANY workspace, then re-run "
                    "the target task (force=True) — identical output refs "
                    "prove the re-derivation"}


def import_bundle(weft, path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise WeftError("data.missing", f"no bundle at {path}",
                        stage="staging")
    with tarfile.open(p, "r:gz") as tar:
        man = json.loads(tar.extractfile("bundle/manifest.json").read())
        if man.get("schema") != SCHEMA:
            raise WeftError("task.invalid",
                            f"not a {SCHEMA} bundle", stage="staging")
        # the host's sealed envelope, verbatim: json in, json out;
        # bytes in, bytes out. None = no envelope was supplied.
        host_meta = None
        names = set(tar.getnames())
        if "bundle/host-metadata.json" in names:
            host_meta = json.loads(
                tar.extractfile("bundle/host-metadata.json").read())
        elif "bundle/host-metadata.bin" in names:
            host_meta = tar.extractfile("bundle/host-metadata.bin").read()
        with tempfile.TemporaryDirectory() as td:
            tar.extractall(td, filter="data")
            import hashlib
            blob_dir = Path(td) / "bundle" / "blobs"
            for blob in (sorted(blob_dir.glob("*"))
                         if blob_dir.exists() else []):
                # content-addressed: the filename IS the claim — verify it
                h = hashlib.sha256()
                with open(blob, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                if h.hexdigest() != blob.name:
                    raise WeftError("data.verify_failed",
                                    f"bundle blob {blob.name} corrupt",
                                    stage="staging")
                dst = weft.cas._blob_path(blob.name)
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    import shutil
                    shutil.copy2(blob, dst)
            tree_dir = Path(td) / "bundle" / "trees"
            for tj in (sorted(tree_dir.glob("*.json"))
                       if tree_dir.exists() else []):
                dst = weft.cas.root / "trees" / tj.name
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    import shutil
                    shutil.copy2(tj, dst)
            for ref, row in man["datarefs"].items():
                if not row:
                    continue
                weft.store.put_dataref(ref, row.get("kind", "file"),
                                       row.get("bytes", 0),
                                       row.get("chunks"),
                                       meta=row.get("meta") or {})
                weft.store.set_location(ref, "@workspace",
                                        str(weft.cas.root))

    for eid, e in man["envs"].items():
        if e["spec_body"]:
            weft.store.put_spec(e["spec_hash"], e["spec_body"].get(
                "name", "bundled"), e["spec_body"])
        weft.store.put_env(eid, e["spec_hash"], e["canonical"],
                           e["native_lock"], e["manifest"], e["platforms"],
                           weakly_reproducible=bool(
                               e.get("weakly_reproducible")))
        if e.get("parent_env_id"):
            weft.store.set_env_parent(eid, e["parent_env_id"],
                                      layerable=bool(e.get("layerable")))

    target = man["jobs"][man["target_job"]]
    weft.store.audit_log(None, "bundle.import", command=man["target_job"],
                         result=path)
    return {
        "target_job": man["target_job"],
        "metadata": host_meta,
        "task": {k: v for k, v in target["task"].items()
                 if k not in ("site",)},
        "recorded_outputs": [
            {"path": o["path"], "ref": o["ref"]}
            for o in (target["manifest"] or {}).get("outputs", [])],
        "envs": sorted(man["envs"]),
        "reproducibility": man.get("reproducibility"),
        "note": "environments and inputs are loaded; task_submit the "
                "returned task on any site (force=True) and compare output "
                "refs against recorded_outputs",
    }
