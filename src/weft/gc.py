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
    """Every ref any job touched: inputs, code, outputs. Conservative."""
    pinned: set[str] = set()
    for j in store.jobs_where():
        task = j["task"]
        for i in (task.get("inputs") or []):
            pinned.add(i["ref"])
        if task.get("code"):
            pinned.add(task["code"]["ref"])
        for o in (j["manifest"] or {}).get("outputs", []):
            pinned.add(o["ref"])
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
             "idle_days": round((now - r["updated_at"]) / 86400, 1)}
            for r in weft.store.realizations_for_site(name)
            if r["state"] == "ready" and r["updated_at"] < cutoff
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
        out["sites"][name] = {
            "idle_days_policy": idle_days,
            "evictable_realizations": realizations,
            # remote caches: pins are advisory (re-stageable); local CAS
            # blobs of pinned refs are never listed for the workspace
            "evictable_refs": stale_refs,
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
        weft.store.audit_log("user", "gc.sweep", site=site,
                             result=f"bytes={evicted}")
        weft.store.emit("gc.swept", site=site, bytes=evicted)
        return {"site": site, "evicted_bytes": evicted,
                "note": "unpinned workspace refs removed; pinned provenance "
                        "content untouched"}
    adapter = weft.adapters[site]
    import shlex
    evicted_envs = 0
    for r in p["evictable_realizations"]:
        adapter.run_cmd(f"rm -rf {shlex.quote(adapter.path(r['location']))}")
        weft.store.set_realization(r["env_id"], site, "?", r["location"],
                                   "evicted", log="gc sweep")
        evicted_envs += 1
    evicted_bytes = 0
    endpoint = adapter.transfer_endpoint()
    for r in p["evictable_refs"]:
        digest = r["ref"].split(":")[-1]
        adapter.run_cmd(
            f"rm -f {shlex.quote(endpoint['cas_root'])}/{digest[:2]}/{digest}")
        weft.store.demote_location(r["ref"], site)
        evicted_bytes += r["bytes"]
    weft.store.audit_log("user", "gc.sweep", site=site,
                         result=f"envs={evicted_envs} bytes={evicted_bytes}")
    weft.store.emit("gc.swept", site=site, realizations=evicted_envs,
                    bytes=evicted_bytes)
    return {"site": site, "evicted_realizations": evicted_envs,
            "evicted_bytes": evicted_bytes,
            "note": "evicted content re-stages/rebuilds automatically on "
                    "next use — correctness is unaffected"}
