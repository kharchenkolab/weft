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


def evict(weft, env_id: str, site: str, archive: bool = False) -> dict:
    real = weft.store.get_realization(env_id, site)
    if not real or real["state"] != "ready":
        return {"env_id": env_id, "site": site,
                "state": (real or {}).get("state", "absent"),
                "note": "nothing realized here to evict"}
    adapter = weft.adapters[site]
    rel = env_dir_rel(env_id)
    freed = real["bytes"] or 0

    archived = None
    if archive:
        archived = _archive_to_controller(weft, env_id, site, adapter)

    adapter.run_cmd(f"rm -rf {shlex.quote(adapter.path(rel))}")
    weft.store.set_realization(
        env_id, site, real["strategy"], rel, "evicted",
        log=f"evicted {'with archive' if archived else '(cache-warm rebuild)'}")
    weft.store.audit_log("user", "env.evict", site=site, command=env_id,
                         result=f"freed={freed} archived={bool(archived)}")
    weft.store.emit("env.evicted", env_id=env_id, site=site,
                    freed_bytes=freed, archived=bool(archived))
    out = {
        "env_id": env_id, "site": site, "state": "evicted",
        "freed_bytes": freed,
        "rebuild": "seconds, offline (site package cache is warm)"
        if not archived else "offline from the controller's archive",
    }
    if archived:
        out["archive_ref"] = archived
    return out


def _archive_to_controller(weft, env_id: str, site: str, adapter) -> str | None:
    """Pack the env from its lock and keep the blob on the CONTROLLER, so
    the site reclaims ~everything and can still rebuild with no internet."""
    env_row = weft.store.get_env(env_id)
    if not env_row:
        raise WeftError("task.invalid", f"unknown EnvID: {env_id}",
                        stage="realize")
    if not weft.pixi_pack:
        raise WeftError(
            "env.realize_failed",
            "archive=True needs the pixi-pack tool on the controller",
            stage="realize",
            hints={"suggestion": "install pixi-pack next to pixi, or evict "
                                 "without archive (rebuild uses the site's "
                                 "package cache)"})
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
    from .capability import compute_view
    caps = (weft.store.get_site(site) or {}).get("capabilities") or {}
    view = compute_view(caps)
    plat = "linux-aarch64" if view.get("arch") in ("arm64", "aarch64") \
        else "linux-64"
    with tempfile.TemporaryDirectory(prefix="weft-archive-") as td:
        tdp = Path(td)
        (tdp / "pixi.toml").write_text(env_row["manifest"])
        (tdp / "pixi.lock").write_text(env_row["native_lock"])
        out_tar = tdp / "environment.tar"
        r = subprocess.run(
            [weft.pixi_pack, "--environment", "default", "--platform", plat,
             "--output-file", str(out_tar), str(tdp)],
            capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not out_tar.exists():
            raise WeftError(
                "env.realize_failed", "archiving (pixi-pack) failed",
                stage="realize",
                hints={"log_tail": (r.stderr or r.stdout)[-1000:]})
        info = weft.cas.register_file(out_tar)
    weft.store.put_dataref(info.ref, "file", info.bytes, info.chunks,
                           meta={"origin": f"archive:{env_id}",
                                 ARCHIVE_META: env_id})
    weft.store.set_location(info.ref, "@workspace", str(weft.cas.root))
    return info.ref


def gc_packages(weft, site: str, confirm: bool = False) -> dict:
    """The SHARED package cache — evict archives no ready realization needs.

    This is the consequential verb: after it, a rebuild of an evicted env
    needs the index (or an archive). `env_evict` alone leaves rebuilds at
    seconds; this is what trades that away for disk.
    """
    adapter = weft.adapters[site]
    r = adapter.run_cmd(
        f"du -sb {shlex.quote(adapter.path('cache/pixi'))} 2>/dev/null | cut -f1",
        timeout=300)
    cache_bytes = int(r.out.strip().split()[0]) if r.out.strip() else 0
    ready = [x for x in weft.store.realizations_for_site(site)
             if x["state"] == "ready"]
    if not confirm:
        return {
            "site": site, "cache_bytes": cache_bytes,
            "ready_realizations": len(ready),
            "note": "pass confirm=true to clear the shared package cache. "
                    "WARNING: rebuilds of evicted envs will then need index "
                    "access (or an archived blob) — with the cache warm they "
                    "take seconds and no network.",
        }
    adapter.run_cmd(f"rm -rf {shlex.quote(adapter.path('cache/pixi'))}")
    weft.store.audit_log("user", "gc.packages", site=site,
                         result=f"freed={cache_bytes}")
    weft.store.emit("gc.packages", site=site, freed_bytes=cache_bytes)
    return {"site": site, "freed_bytes": cache_bytes,
            "note": "shared cache cleared; realized envs still work (they "
                    "hardlink), but rebuilds now need the index"}


def footprint(weft, site: str) -> dict:
    """What actually occupies the site — the number a GC policy needs."""
    adapter = weft.adapters[site]

    def du(rel: str) -> int:
        r = adapter.run_cmd(
            f"du -sb {shlex.quote(adapter.path(rel))} 2>/dev/null | cut -f1",
            timeout=300)
        try:
            return int(r.out.strip().split()[0])
        except (ValueError, IndexError):
            return 0

    envs = du("envs")
    cache = du("cache")
    cas = du("cas")
    reals = []
    for x in weft.store.realizations_for_site(site):
        if x["state"] != "ready":
            continue
        reals.append({
            "env_id": x["env_id"], "bytes": x["bytes"],
            "last_used": x["last_used"],
            "idle_days": round((time.time() - (x["last_used"] or 0)) / 86400, 1)
            if x["last_used"] else None,
        })
    reals.sort(key=lambda r: r["last_used"] or 0)
    return {
        "site": site,
        "prefixes_bytes": envs, "package_cache_bytes": cache,
        "data_cache_bytes": cas,
        "realizations": reals,
        "note": "evicting prefixes reclaims prefixes_bytes and leaves "
                "rebuilds at seconds (the package cache stays warm); "
                "gc_packages additionally reclaims package_cache_bytes but "
                "makes rebuilds need the index",
    }
