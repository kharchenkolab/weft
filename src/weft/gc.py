"""Garbage collection (docs 03 §6 / 04 §7): policy-driven, conservative.

Rules, as designed: candidates are things unused for `gc_idle_days`
(site policy, default 14) AND not pinned; refs reachable from any job's
provenance (inputs or outputs) are pin-protected on the local CAS (the
record must survive) and advisory-only on remote caches (reconstructible
by re-staging). Nothing is EVER deleted implicitly — `gc_plan` is a dry
run; `gc_sweep(confirm=True)` executes it, audited.
"""

from __future__ import annotations

import time
from pathlib import Path

from .errors import WeftError
from .policy import site_policy

DEFAULT_IDLE_DAYS = 14.0


def _pinned_refs(store) -> set[str]:
    """Every ref the record depends on: job provenance (inputs, code,
    outputs), eviction archives, captured installer inputs, compile-cache
    artifacts. Conservative — losing any of these silently breaks a
    rebuild-anywhere or reproduce-this claim the record already made."""
    pinned: set[str] = set()
    for j in store.jobs_where():
        task = j["task"]
        for i in (task.get("inputs") or []):
            pinned.add(i["ref"])
        if task.get("code"):
            pinned.add(task["code"]["ref"])
        for o in (j["manifest"] or {}).get("outputs", []):
            pinned.add(o["ref"])
    # blobs referenced only from dataref meta, not job provenance
    for r in store.all_datarefs():
        meta = r.get("meta") or {}
        if ("archived_blob" in meta or "compile_cache" in meta
                or str(meta.get("origin", "")).startswith("post_install:")):
            pinned.add(r["ref"])
    # captured post_install inputs, referenced from env specs
    for row in store.list_envs():
        env = store.get_env(row["env_id"])
        extras = (env.get("canonical") or {}).get("extras", {}) if env else {}
        for inp in extras.get("post_install_inputs") or []:
            if inp.get("ref"):
                pinned.add(inp["ref"])
    return pinned


def plan(weft, site: str | None = None) -> dict:
    """What could be reclaimed, per site — dry, free, honest."""
    now = time.time()
    out: dict = {"sites": {}, "note": "dry run; gc_sweep(site, confirm=True) "
                                      "executes — nothing is implicit"}
    pinned = _pinned_refs(weft.store)
    targets = [site] if site else ["@workspace", *weft.adapters]
    for name in targets:
        if name == "@workspace":
            # the local CAS: pinned refs (provenance-reachable) are NEVER
            # candidates here — the record must survive (doc 04 §7)
            cutoff_ws = now - DEFAULT_IDLE_DAYS * 86400
            stale = []
            for ref in weft.store.refs_present_at("@workspace"):
                if ref in pinned:
                    continue
                locs = [l for l in weft.store.locations_of(ref)
                        if l["site"] == "@workspace"]
                if locs and locs[0]["verified_at"] < cutoff_ws:
                    d = weft.store.get_dataref(ref)
                    stale.append({"ref": ref,
                                  "bytes": d["bytes"] if d else 0})
            out["sites"]["@workspace"] = {
                "idle_days_policy": DEFAULT_IDLE_DAYS,
                "evictable_refs": stale,
                "pinned_protected": True,
                "reclaimable_bytes": sum(r["bytes"] for r in stale),
            }
            continue
        row = weft.store.get_site(name) or {}
        idle_days = float(site_policy(row).get("gc_idle_days",
                                               DEFAULT_IDLE_DAYS))
        cutoff = now - idle_days * 86400
        realizations = [
            {"env_id": r["env_id"], "location": r["location"],
             "idle_days": round(
                 (now - max(r["updated_at"], r["last_used"] or 0)) / 86400, 1)}
            for r in weft.store.realizations_for_site(name)
            if r["state"] == "ready"
            and not r.get("read_only")     # not ours to reclaim
            # recency = actual use, not last state change: an env realized
            # long ago but used hourly is HOT, not idle
            and max(r["updated_at"], r["last_used"] or 0) < cutoff
        ]
        stale_refs = []
        for ref in weft.store.refs_present_at(name):
            locs = [l for l in weft.store.locations_of(ref)
                    if l["site"] == name]
            if locs and locs[0]["verified_at"] < cutoff:
                d = weft.store.get_dataref(ref)
                stale_refs.append({"ref": ref,
                                   "bytes": d["bytes"] if d else 0,
                                   "pinned_locally": ref in pinned})
        # retention.md R4: dead runs' SANDBOXES past their TTL. Retained
        # files are unaffected (they live elsewhere, or the surviving
        # hardlink keeps the inode); the terminal inventory (knowledge)
        # is never a candidate for anything.
        remains_days = float(site_policy(row).get("run_remains_days", 14))
        remains_cutoff = now - remains_days * 86400
        remains = []
        for job in weft.store.jobs_where(site=name):
            if job["state"] in ("DONE", "FAILED", "CANCELLED") \
                    and job["updated_at"] < remains_cutoff:
                remains.append({"target": job["job_id"],
                                "jobdir": f"jobs/{job['job_id']}",
                                "age_days": round(
                                    (now - job["updated_at"]) / 86400, 1)})
        for k in weft.store.list_kernels():
            if k["site"] == name and k["state"] in ("stopped", "died") \
                    and k["last_used"] < remains_cutoff:
                remains.append({"target": k["kernel_id"],
                                "jobdir": k["jobdir"],
                                "age_days": round(
                                    (now - k["last_used"]) / 86400, 1)})
        out["sites"][name] = {
            "idle_days_policy": idle_days,
            "evictable_realizations": realizations,
            # remote caches: pins are advisory (re-stageable); local CAS
            # blobs of pinned refs are never listed for the workspace
            "evictable_refs": stale_refs,
            "run_remains_days_policy": remains_days,
            "run_remains": remains,
            "reclaimable_bytes": sum(r["bytes"] for r in stale_refs),
        }
    return out


def sweep(weft, site: str, confirm: bool = False) -> dict:
    p = plan(weft, site)["sites"].get(site)
    if p is None:
        raise WeftError("task.invalid", f"unknown site: {site}", stage="infra")
    if not confirm:
        return {"site": site, **p,
                "note": "pass confirm=true to execute this plan"}
    if site == "@workspace":
        evicted = 0
        for r in p["evictable_refs"]:
            digest = r["ref"].split(":")[-1]
            blob = weft.cas.root / digest[:2] / digest
            tree = weft.cas.root / "trees" / f"{digest}.json"
            for path in (blob, tree):
                if path.exists():
                    path.unlink()
            weft.store.demote_location(r["ref"], "@workspace")
            evicted += r["bytes"]
        weft.store.audit_log(None, "gc.sweep", site=site,
                             result=f"bytes={evicted}")
        weft.store.emit("gc.swept", site=site, bytes=evicted)
        return {"site": site, "evicted_bytes": evicted,
                "note": "unpinned workspace refs removed; pinned provenance "
                        "content untouched"}
    evicted_envs, skipped = 0, []
    for r in p["evictable_realizations"]:
        # through evict(), NOT a raw rm: the in-use and overlay-dependent
        # guards apply to a sweep exactly as to an explicit evict
        from . import evict as _evict
        try:
            _evict.evict(weft, r["env_id"], site, cascade=False)
            evicted_envs += 1
        except WeftError as e:
            skipped.append({"env_id": r["env_id"], "why": e.code})
    adapter = weft.adapters[site]
    import shlex
    evicted_bytes = 0
    endpoint = adapter.transfer_endpoint()
    for r in p["evictable_refs"]:
        digest = r["ref"].split(":")[-1]
        adapter.run_cmd(
            f"rm -f {shlex.quote(endpoint['cas_root'])}/{digest[:2]}/{digest}")
        weft.store.demote_location(r["ref"], site)
        evicted_bytes += r["bytes"]
    swept_remains = 0
    for r in p.get("run_remains") or []:
        pin = weft.store.get_retained(r["target"])
        if pin and pin["state"] == "pinned-pending":
            # a stuck pin must not become silent data loss via the
            # janitor — skip loudly; settle or forget it explicitly
            weft.store.emit("run.remains_skipped", target=r["target"],
                            site=site, why="pinned-pending retain")
            continue
        weft.store.emit("run.remains_swept", target=r["target"],
                        site=site, age_days=r["age_days"])
        adapter.run_cmd(f"rm -rf {shlex.quote(adapter.path(r['jobdir']))}",
                        timeout=1800)
        swept_remains += 1
    weft.store.audit_log(None, "gc.sweep", site=site,
                         result=f"envs={evicted_envs} bytes={evicted_bytes}"
                                f" remains={swept_remains}")
    weft.store.emit("gc.swept", site=site, realizations=evicted_envs,
                    bytes=evicted_bytes, run_remains=swept_remains)
    out = {"site": site, "evicted_realizations": evicted_envs,
           "evicted_bytes": evicted_bytes,
           "swept_run_remains": swept_remains,
           "note": "evicted content re-stages/rebuilds automatically on "
                   "next use — correctness is unaffected"}
    if skipped:
        out["skipped"] = skipped
        out["note"] += ("; skipped envs are in live use or under overlay "
                        "children (evict them explicitly to see the details)")
    return out
