"""Realization eviction — reclaim GBs without losing the ability to return.

The tiers that actually exist on a site:

  realized prefix   envs/<EnvID>/   GBs, ONE PER ENV
  package cache     cache/pixi/     shared + deduplicated across ALL envs
  the lock          (controller)    kilobytes

So dropping a prefix is cheap to undo: with the cache warm, `pixi install
--frozen` is a hardlink forest — seconds, no network. That makes
"strip aggressively, re-materialize on demand" the right default posture,
and it is why a per-env "packed blob" tier is NOT offered: it would
duplicate, per env, what the site already holds shared.

Two verbs, therefore:

  env_evict(env_id, site)                drop the prefix; cache stays
  env_evict(env_id, site, archive=True)  + pack the env and store the blob
                                         ON THE CONTROLLER — reclaims ~100%
                                         of site space and still rebuilds
                                         with no site network (air-gapped)
  gc_packages(site)                      the shared cache, separately: the
                                         consequential one, since it is what
                                         makes a rebuild need the index
"""

from __future__ import annotations

import shlex
import time

from .errors import WeftError
from .realize import env_dir_rel

ARCHIVE_META = "archived_blob"


def evict(weft, env_id: str, site: str, archive: bool = False,
          cascade: bool = False) -> dict:
    real = weft.store.get_realization(env_id, site)
    if not real or real["state"] != "ready":
        return {"env_id": env_id, "site": site,
                "state": (real or {}).get("state", "absent"),
                "note": "nothing realized here to evict"}
    if real.get("read_only"):
        root = real["location"].rsplit("/envs/", 1)[0]
        raise WeftError(
            "task.invalid",
            "this realization is ADOPTED from a read-only root — not "
            "yours to evict", stage="infra",
            hints={"location": real["location"],
                   "owner_action": f"the owner of {root} manages its "
                                   "lifecycle",
                   "suggestion": "to stop using it, just stop using it — "
                                 "it costs you no disk; your own copy (if "
                                 "any) is what env_evict reclaims"})

    # live work first: a queued/running job, an open session/kernel, or a
    # running service will activate this exact prefix — evicting it under
    # them kills work loudly and pointlessly. (Same-store visibility only:
    # another controller's jobs are invisible here, as everywhere.)
    in_use = []
    for j in weft.store.jobs_where(site=site):
        if j["state"] in ("PENDING", "QUEUED", "RUNNING") \
                and (j.get("task") or {}).get("env") == env_id:
            in_use.append({"kind": "job", "id": j["job_id"],
                           "state": j["state"]})
    for s in weft.store.list_sessions(site):
        if s["state"] == "active" and s.get("base_env_id") == env_id:
            in_use.append({"kind": "session", "id": s["session_id"]})
    for k in weft.store.list_kernels(state="running"):
        if k.get("env_id") == env_id and k.get("site") == site:
            in_use.append({"kind": "kernel", "id": k["kernel_id"]})
    if in_use:
        raise WeftError(
            "env.evict_blocked",
            f"{len(in_use)} live job(s)/session(s)/kernel(s) on {site} use "
            "this env right now", stage="infra",
            hints={"in_use": in_use,
                   "suggestion": "wait for or cancel/stop them first "
                                 "(task_cancel / session_stop / "
                                 "kernel_stop), then evict"})

    # an overlay child borrows this prefix's bytes at runtime: evicting the
    # parent out from under it would break a "ready" env. Refuse by default;
    # cascade=True evicts the dependents first (they rebuild in seconds).
    dependents = [
        c for c in weft.store.children_of_env(env_id)
        if (cr := weft.store.get_realization(c, site))
        and cr["strategy"] == "overlay"
        and cr["state"] in ("ready", "building")
    ]
    if dependents and not cascade:
        raise WeftError(
            "env.evict_blocked",
            f"{len(dependents)} overlay env(s) on {site} stack on this "
            "prefix and would break",
            stage="infra",
            hints={"dependents": dependents,
                   "suggestion": "evict the dependents first, or pass "
                                 "cascade=True to evict them with the parent "
                                 "(all rebuild cache-warm in seconds)"})
    cascaded = [evict(weft, c, site, archive=False, cascade=True)
                for c in dependents]

    adapter = weft.adapters[site]
    rel = env_dir_rel(env_id)

    archived = None
    if archive:
        archived = _archive_to_controller(weft, env_id, site, adapter)

    # measure what is ACTUALLY reclaimed: a prefix hardlinks most of its
    # bytes from the shared package cache, so its apparent size (du) is not
    # what the filesystem gets back (live-agent eval: 2.4x overstatement)
    before = _free_bytes(adapter)
    apparent = real["bytes"] or 0
    rm = adapter.run_cmd(f"rm -rf {shlex.quote(adapter.path(rel))} && "
                         f"echo WEFT_RM_OK")
    if "WEFT_RM_OK" not in (rm.out or ""):
        raise WeftError(
            "env.realize_failed",
            f"eviction rm failed on {site} (permissions? busy mount?)",
            stage="infra", hints={"log_tail": (rm.err or rm.out)[-500:]})
    after = _free_bytes(adapter)
    measured = bool(before and after)
    freed = max(0, after - before) if measured else apparent
    weft.store.set_realization(
        env_id, site, real["strategy"], rel, "evicted",
        log=f"evicted {'with archive' if archived else '(cache-warm rebuild)'}")
    weft.store.audit_log(None, "env.evict", site=site, command=env_id,
                         result=f"freed={freed} archived={bool(archived)}")
    weft.store.emit("env.evicted", env_id=env_id, site=site,
                    freed_bytes=freed, archived=bool(archived))
    out = {
        "env_id": env_id, "site": site, "state": "evicted",
        "freed_bytes": freed,
        # honest accounting or honest labels — never a du number wearing a
        # df caption (and the df window can catch concurrent writes: it is
        # a measurement, not a ledger)
        "freed_measurement": "filesystem (df delta; concurrent activity "
                             "lands in the same window)" if measured
        else "apparent size (df unavailable — likely overstated: most "
             "prefix bytes are hardlinks into the shared cache)",
        "apparent_bytes": apparent,
        "note": "freed_bytes is measured from the filesystem; apparent_bytes "
                "is the prefix's size, most of which is hardlinked from the "
                "shared package cache and therefore not reclaimed by this"
        if measured and apparent > freed else None,
        "rebuild": "seconds, offline (site package cache is warm)"
        if not archived else "offline from the controller's archive",
    }
    if archived:
        caveats = _archive_caveats(weft, env_id)
        if caveats:
            out["rebuild"] = ("from the controller's archive; NOT fully "
                              "offline: " + "; ".join(caveats))
            out["archive_caveats"] = caveats
    if archived:
        out["archive_ref"] = archived
    if cascaded:
        out["cascaded"] = [{"env_id": c["env_id"],
                            "freed_bytes": c["freed_bytes"]}
                           for c in cascaded]
    return out


def _archive_caveats(weft, env_id: str) -> list[str]:
    """What the archive blob does NOT freeze. The blob holds pixi-pack's
    conda/pypi artifacts; anything outside that re-executes or re-packs at
    rebuild time — say so instead of overpromising 'offline'."""
    env_row = weft.store.get_env(env_id) or {}
    extras = (env_row.get("canonical") or {}).get("extras", {})
    caveats = []
    if extras.get("post_install"):
        n_inputs = len(extras.get("post_install_inputs") or [])
        n_steps = len(extras["post_install"])
        if n_inputs < n_steps:
            caveats.append(
                f"{n_steps} post_install step(s) re-execute on the site at "
                "rebuild and may fetch from the network "
                f"({n_inputs} have captured inputs)")
    layers = (env_row.get("canonical") or {}).get("layers") or {}
    if layers:
        caveats.append(
            f"{sorted(layers)} layer(s) re-pack from the controller's index "
            "at rebuild (the archive covers conda/pypi only)")
    return caveats


def _free_bytes(adapter) -> int:
    # -Pk is POSIX (1024-byte blocks); GNU-only -B1 breaks on BSD/darwin df
    r = adapter.run_cmd(
        f"df -Pk {shlex.quote(adapter.root)} 2>/dev/null | awk 'NR==2{{print $4}}'",
        timeout=60)
    try:
        return int(r.out.strip()) * 1024
    except (ValueError, AttributeError):
        return 0


def _archive_to_controller(weft, env_id: str, site: str, adapter) -> str | None:
    """Pack the env from its lock and keep the blob on the CONTROLLER, so
    the site reclaims ~everything and can still rebuild with no internet."""
    env_row = weft.store.get_env(env_id)
    if not env_row:
        raise WeftError("task.invalid", f"unknown EnvID: {env_id}",
                        stage="realize")
    pixi_pack = weft.pixi_pack
    if not pixi_pack:
        # no sibling binary next to pixi: fetch the pinned release for
        # the controller's platform (cached once under site-tools)
        try:
            from .site_tools import fetch_tool
            from .spec import current_platform
            pixi_pack = str(fetch_tool("pixi-pack", current_platform()))
        except WeftError:
            raise WeftError(
                "env.realize_failed",
                "archive=True needs the pixi-pack tool on the controller",
                stage="realize",
                hints={"suggestion": "install pixi-pack next to pixi, or "
                                     "evict without archive (rebuild uses "
                                     "the site's package cache)"})
    layers = env_row["canonical"].get("layers") or {}
    unpackable = [eco for eco in layers
                  if not hasattr(weft.envman.solvers.get(eco), "pack_layer")]
    if unpackable:
        raise WeftError(
            "env.unsatisfiable_on_site",
            f"cannot archive: the {unpackable} layer(s) have no packer",
            stage="realize",
            hints={"suggestion": "evict without archive (the rebuild will "
                                 "need index access for those layers)"})
    import subprocess
    import tempfile
    from pathlib import Path
    from .realize import _site_platform
    caps = (weft.store.get_site(site) or {}).get("capabilities") or {}
    plat = _site_platform(caps)
    with tempfile.TemporaryDirectory(prefix="weft-archive-") as td:
        tdp = Path(td)
        (tdp / "pixi.toml").write_text(env_row["manifest"])
        (tdp / "pixi.lock").write_text(env_row["native_lock"])
        out_tar = tdp / "environment.tar"
        r = subprocess.run(
            [pixi_pack, "--environment", "default", "--platform", plat,
             "--output-file", str(out_tar), str(tdp)],
            capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not out_tar.exists():
            raise WeftError(
                "env.realize_failed", "archiving (pixi-pack) failed",
                stage="realize",
                hints={"log_tail": (r.stderr or r.stdout)[-1000:]})
        info = weft.cas.register_file(out_tar)
    meta = {"origin": f"archive:{env_id}", ARCHIVE_META: env_id,
            "platform": plat}
    # without the plain hash, remote transfers of chunked (>64MB) blobs
    # verify against the merkle root and always fail — every real env
    # archive is chunked
    if info.plain_sha256:
        meta["sha256_plain"] = info.plain_sha256
    weft.store.put_dataref(info.ref, "file", info.bytes, info.chunks,
                           meta=meta)
    weft.store.set_location(info.ref, "@workspace", str(weft.cas.root))
    return info.ref


def gc_packages(weft, site: str, confirm: bool = False) -> dict:
    """The SHARED package cache — evict archives no ready realization needs.

    This is the consequential verb: after it, a rebuild of an evicted env
    needs the index (or an archive). `env_evict` alone leaves rebuilds at
    seconds; this is what trades that away for disk.
    """
    from .runner_util import du_apparent_bytes_cmd
    adapter = weft.adapters[site]
    r = adapter.run_cmd(
        du_apparent_bytes_cmd(shlex.quote(adapter.path("cache/pixi"))),
        timeout=300)
    cache_bytes = int(r.out.strip().split()[0]) if r.out.strip() else 0
    ready = [x for x in weft.store.realizations_for_site(site)
             if x["state"] == "ready"]
    if not confirm:
        return {
            "site": site,
            "cache_bytes": cache_bytes,
            "cache_bytes_note": "apparent (du): blocks still hardlinked by "
                                "realized prefixes are NOT freed by clearing",
            "ready_realizations": len(ready),
            "note": "pass confirm=true to clear the shared package cache. "
                    "WARNING: rebuilds of evicted envs will then need index "
                    "access (or an archived blob) — with the cache warm they "
                    "take seconds and no network.",
        }
    building = [x["env_id"] for x in weft.store.realizations_for_site(site)
                if x["state"] == "building"]
    if building:
        raise WeftError(
            "state.conflict",
            f"{len(building)} env build(s) on {site} are hardlinking from "
            "this cache right now", stage="infra",
            hints={"building": building,
                   "suggestion": "wait for the builds to finish, then re-run"})
    before = _free_bytes(adapter)
    adapter.run_cmd(f"rm -rf {shlex.quote(adapter.path('cache/pixi'))}")
    after = _free_bytes(adapter)
    freed = max(0, after - before) if (before and after) else cache_bytes
    weft.store.audit_log(None, "gc.packages", site=site,
                         result=f"freed={freed}")
    weft.store.emit("gc.packages", site=site, freed_bytes=freed)
    return {"site": site, "freed_bytes": freed,
            "apparent_bytes": cache_bytes,
            "note": "shared cache cleared; realized envs still work (they "
                    "hardlink — which is why freed_bytes can be far below "
                    "apparent_bytes), but rebuilds now need the index"}


def footprint(weft, site: str) -> dict:
    """What actually occupies the site — the number a GC policy needs."""
    adapter = weft.adapters[site]

    def du(rel: str) -> int:
        from .runner_util import du_apparent_bytes_cmd
        r = adapter.run_cmd(
            du_apparent_bytes_cmd(shlex.quote(adapter.path(rel))),
            timeout=300)
        try:
            return int(r.out.strip().split()[0])
        except (ValueError, IndexError):
            return 0

    envs = du("envs")
    cache = du("cache")
    cas = du("cas")
    reals = []
    known = set()
    for x in weft.store.realizations_for_site(site):
        if x["state"] in ("ready", "building"):
            known.add(x["env_id"].rsplit(":", 1)[-1])
        if x["state"] != "ready":
            continue
        ro = bool(x.get("read_only"))
        reals.append({
            "env_id": x["env_id"], "bytes": x["bytes"],
            "last_used": x["last_used"],
            "idle_days": round((time.time() - (x["last_used"] or 0)) / 86400, 1)
            if x["last_used"] else None,
            # everything OWNED here is evictable: rebuilds are cheap. The
            # host's policy decides WHICH — weft just refuses to hide the
            # option. Read-only adoptions cost the caller nothing and are
            # not theirs to reclaim.
            "evictable": not ro,
            **({"read_only": True} if ro else {}),
        })
    reals.sort(key=lambda r: r["last_used"] or 0)

    from .policy import site_policy
    site_row = weft.store.get_site(site) or {}
    shared = bool(((site_row.get("config")) or {}).get("shared"))
    grace_min = float(site_policy(site_row).get("orphan_grace_minutes", 60))

    def recently_touched(rel: str) -> bool:
        # concurrent work this store can't see (another workspace's build,
        # a neighbor's session) shows up as fresh mtimes — grace it
        if grace_min <= 0:
            return False
        r = adapter.run_cmd(
            f"find {shlex.quote(adapter.path(rel))} "
            f"-newermt '-{int(grace_min)} minutes' "
            f"2>/dev/null | head -1", timeout=60)
        return bool(r.out.strip())

    # dirs weft left behind that no record claims (crashed sessions, stale
    # kernels): the live-agent eval found 174 MB of these unreclaimable
    orphans = []
    session_names = {s["location"].rsplit("/", 1)[-1]
                     for s in weft.store.list_sessions(site)}
    kernel_names = {k["jobdir"].rsplit("/", 1)[-1]
                    for k in weft.store.list_kernels()}
    for area, live, ours in (
        ("envs", known, None),
        ("sessions", {s["location"].rsplit("/", 1)[-1]
                      for s in weft.store.list_sessions(site)
                      if s["state"] == "active"}, session_names),
        ("kernels", {k["jobdir"].rsplit("/", 1)[-1]
                     for k in weft.store.list_kernels(state="running")},
         kernel_names),
    ):
        r = adapter.run_cmd(
            f"ls {shlex.quote(adapter.path(area))} 2>/dev/null", timeout=60)
        for entry in r.out.split():
            name = entry.rstrip("/")
            if name in live or name.endswith(".lease"):
                continue
            if area == "envs":
                # a marker is a valid claim by SOMEONE (adoptable by content
                # address — never garbage); no marker + recent writes is a
                # build in progress from a store we can't see
                if adapter.file_exists(f"envs/{name}/.weft-ready"):
                    continue
            elif shared and ours is not None and name not in ours:
                # a shared root holds other users' sessions/kernels — not
                # ours to judge, and not counted as reclaimable
                continue
            if recently_touched(f"{area}/{name}"):
                continue
            orphans.append({"area": area, "name": name,
                            "bytes": du(f"{area}/{name}")})
    orphan_bytes = sum(o["bytes"] for o in orphans)

    return {
        "site": site,
        "free_bytes": _free_bytes(adapter),
        "prefixes_bytes": envs, "package_cache_bytes": cache,
        "data_cache_bytes": cas,
        "bytes_note": "area sizes are apparent (du) and share hardlinked "
                      "blocks — they sum to MORE than the disk they occupy; "
                      "free_bytes is the filesystem's own number",
        "orphan_bytes": orphan_bytes, "orphans": orphans[:20],
        "realizations": reals,
        "note": "every ready realization is evictable (env_evict) — rebuilds "
                "are seconds while the package cache stays warm. gc_packages "
                "additionally reclaims package_cache_bytes but makes rebuilds "
                "need the index. gc_orphans clears leftovers no record claims.",
    }


def gc_orphans(weft, site: str, confirm: bool = False) -> dict:
    """Directories weft left behind that no record claims — crashed session
    clones, stale kernel sandboxes, evicted-but-not-removed env dirs."""
    fp = footprint(weft, site)
    if not confirm:
        return {"site": site, "orphans": fp["orphans"],
                "orphan_bytes": fp["orphan_bytes"],
                "note": "pass confirm=true to remove these"}
    adapter = weft.adapters[site]
    freed = 0
    for o in fp["orphans"]:
        adapter.run_cmd(
            f"rm -rf {shlex.quote(adapter.path(o['area'] + '/' + o['name']))}")
        freed += o["bytes"]
    weft.store.audit_log(None, "gc.orphans", site=site,
                         result=f"freed={freed}")
    weft.store.emit("gc.orphans", site=site, freed_bytes=freed,
                    count=len(fp["orphans"]))
    return {"site": site, "removed": len(fp["orphans"]), "freed_bytes": freed}
