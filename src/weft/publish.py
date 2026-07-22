"""Institutional publishing: squashfs envs in shared read-only trees.

The truth stays content-addressed and immutable — published realizations
live at `{tree}/envs/<hash>` and are never edited in place (users may be
running against the mounted image right now; their overlays pin the
exact parent EnvID). The HUMAN layer is a thin catalog: name → versions
→ EnvID, like tags pointing at commits. The catalog also stores each
env's spec + lock so consumers adopt by NAME without re-solving —
re-solving decays (the index moves, the EnvID diverges, adoption
silently misses and a multi-GB env rebuilds privately).

Publish is a REBUILD at the destination, not a copy: conda envs bake
absolute paths, so the content must be built at the very path every
consumer will mount. The site package cache makes this cheap after a
test build (downloads dedupe; it is a link + squash pass).

Retirement is graceful by construction: `unpublish` removes the catalog
pointer and LEAVES the directory (a deleted image would not even kill
running jobs — squashfuse holds the fd — but overlays and new mounts
need the grace period); `purge=True` deletes after.
"""

from __future__ import annotations

import json
import re
import shlex
import time
import uuid

from .errors import WeftError

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _glibc_floor(native_lock: str) -> str | None:
    """Highest __glibc requirement in the lock — the oldest linux the
    published env can RUN on."""
    floors = re.findall(r"__glibc[^0-9]{0,8}>=\s*'?([0-9]+(?:\.[0-9]+)+)",
                        native_lock or "")
    if not floors:
        return None
    return max(floors, key=lambda v: tuple(int(x) for x in v.split(".")))


def _catalog_path(tree: str) -> str:
    return f"{tree.rstrip('/')}/catalog.json"


def _read_catalog(adapter, tree: str) -> dict:
    try:
        return json.loads(adapter.read_file(_catalog_path(tree)).decode())
    except WeftError:
        return {"catalog_version": 1, "envs": {}}
    except ValueError:
        # corrupt content at use — state.conflict said "wait and retry",
        # which can never fix a damaged file (2026-07 sweep #21)
        raise WeftError(
            "data.verify_failed",
            f"catalog at {_catalog_path(tree)} is not valid JSON",
            stage="infra",
            hints={"suggestion": "inspect/restore the catalog file; weft "
                                 "never repairs a shared tree unasked"})


def _write_catalog(adapter, tree: str, catalog: dict) -> None:
    # atomic replace: consumers may read at any moment
    path = _catalog_path(tree)
    tmp = f"{path}.tmp.{uuid.uuid4().hex[:8]}"
    adapter.write_file(tmp, (json.dumps(catalog, indent=1, sort_keys=True)
                             + "\n").encode())
    r = adapter.run_cmd(f"mv {shlex.quote(tmp)} {shlex.quote(path)}")
    if r.rc != 0:
        # sticky/foreign-owned trees refuse the replace — without this
        # check publish reported success with the catalog UNCHANGED
        # (2026-07 sweep #10)
        adapter.run_cmd(f"rm -f {shlex.quote(tmp)}; true")
        raise WeftError(
            "data.transfer_failed",
            f"could not update the publish catalog at {path}",
            stage="infra",
            hints={"stderr": (r.err or r.out)[-300:],
                   "suggestion": "the tree owner must grant write/replace "
                                 "on the catalog (sticky-bit dirs protect "
                                 "foreign-owned files), or publish to a "
                                 "tree you own"})


def _ensure_unmounted(adapter, mnt_path: str) -> None:
    """A BUSY fusermount -u exits 1 silently and leaves a publisher-owned
    FUSE mount at the published path — EACCES for every other consumer
    (FUSE hides mounts across users). Verify the mount is actually gone
    before the catalog says this env is usable (2026-07 sweep #10)."""
    mnt = shlex.quote(mnt_path)
    adapter.run_cmd(f"fusermount -u {mnt} 2>/dev/null || "
                    f"fusermount3 -u {mnt} 2>/dev/null; true")
    probe = adapter.run_cmd(
        f"grep -qs ' {mnt_path} ' /proc/mounts && echo live || echo clear")
    if probe.out.strip() == "live":
        raise WeftError(
            "state.conflict",
            f"a publisher-owned FUSE mount is still live at {mnt_path}; "
            f"publishing it would EACCES every other consumer",
            stage="infra", retryable=True,
            hints={"suggestion": "something still holds the mount "
                                 "(lsof +D the path); close it and retry "
                                 "the publish"})


def _validate(weft, site: str, tree: str, name: str, version: str) -> tuple:
    if not _NAME_RE.match(name or ""):
        raise WeftError("task.invalid",
                        f"bad publish name {name!r} (letters, digits, ._-)",
                        stage="infra")
    if not _NAME_RE.match(version or ""):
        raise WeftError("task.invalid", f"bad version {version!r}",
                        stage="infra")
    if not tree.startswith("/"):
        raise WeftError("task.invalid", "tree must be an absolute path",
                        stage="infra")
    adapter = weft.adapters.get(site)
    if adapter is None:
        raise WeftError("task.invalid", f"unknown site: {site}", stage="infra",
                        hints={"registered": sorted(weft.adapters)})
    root = adapter.root.rstrip("/")
    t = tree.rstrip("/")
    if t == root or t.startswith(root + "/") or root.startswith(t + "/"):
        raise WeftError(
            "task.invalid",
            "the publish tree must live OUTSIDE the site's weft root "
            "(it is a shared, admin-curated space; the root is private "
            "and GC-managed)", stage="infra",
            hints={"tree": tree, "weft_root": adapter.root})
    return adapter, t


def _staging_plan(caps: dict, site_row: dict, staging: str | None,
                  env_hash: str, tree: str) -> tuple[str | None, str | None]:
    """Where should the build's small-file churn land? Build STORAGE is
    decoupled from build PATH (the prefix is bind-mounted at the tree
    path inside each build command's userns), so a slow netfs tree
    receives one sequential image write instead of ~10^4 small-file ops.
    Returns (staging_rel | None, why_not | None). Precedence: call arg >
    site config `publish_staging` > 'auto' (under the site root — the
    filesystem regular realizations already build on). 'none' keeps the
    classic build-at-destination."""
    import hashlib
    from .capability import compute_view
    choice = str(staging
                 or (site_row.get("config") or {}).get("publish_staging")
                 or "auto")
    if choice.lower() in ("none", "off", "destination"):
        return None, "staging disabled ('none')"
    if not (compute_view(caps or {}).get("squashfs") or {}).get("userns"):
        return None, ("no user namespaces on this site — bind-staging "
                      "needs unshare -rm; building at the destination")
    sub = (f"{env_hash[:24]}-"
           f"{hashlib.sha256(tree.encode()).hexdigest()[:8]}")
    if choice == "auto":
        return f"stage/publish/{sub}", None
    if not choice.startswith("/"):
        raise WeftError(
            "task.invalid",
            f"staging must be an absolute path, 'auto' or 'none' "
            f"(got {choice!r})", stage="infra")
    return f"{choice.rstrip('/')}/{sub}", None


def publish(weft, env_id: str, site: str, tree: str, name: str,
            version: str, notes: str = "", latest: bool = True,
            staging: str | None = None) -> dict:
    """Build `env_id` as a squashfs realization AT {tree}/envs/<hash> and
    point catalog[name][version] at it. Idempotent per (env, dest)."""
    from .capability import squashfs_mode
    from .realize import (_build_squashfs, _marker, _site_platform,
                          _spot_check_and_mark)

    adapter, tree = _validate(weft, site, tree, name, version)
    env_row = weft.store.get_env(env_id)
    if not env_row:
        raise WeftError("task.invalid", f"unknown EnvID: {env_id}",
                        stage="infra",
                        hints={"suggestion": "env_ensure the spec first"})
    site_row = weft.store.get_site(site) or {}
    caps = site_row.get("capabilities") or {}
    if squashfs_mode(caps) is None:
        raise WeftError(
            "env.unsatisfiable_on_site",
            f"{site} cannot build+mount squashfs images", stage="realize",
            hints={"squashfs": caps.get("squashfs") or {}})

    rel = f"{tree}/envs/{env_id.rsplit(':', 1)[-1]}"
    modules = (env_row["canonical"].get("extras") or {}).get("modules") or []
    modules_init = (site_row.get("config") or {}).get("modules_init", "")
    pack_tools = {"pixi_pack": weft.pixi_pack, "cas": weft.cas,
                  "transfers": weft.transfers,
                  "solvers": weft.envman.solvers, "store": weft.store,
                  "dataman": weft.dataman,
                  "site_platform": _site_platform(caps)}

    already = _marker(adapter, rel)
    if already.get("strategy") == "squashfs" \
            and adapter.file_exists(f"{rel}/image.sqfs"):
        # image is content-addressed and stays; the SIDECARS are cheap
        # and regenerating them heals fixes (a shared tree's first second
        # cluster taught us: never bake site-specific paths in them)
        from .realize import _write_squashfs_activation
        _write_squashfs_activation(adapter, rel, modules, modules_init)
        weft.store.emit("env.publish.reused", env_id=env_id, site=site,
                        tree=tree)
        meta = {"image_sha256": already.get("image_sha256"),
                "image_bytes": already.get("image_bytes")}
    else:
        staging_rel, staging_why = _staging_plan(
            caps, site_row, staging, env_id.rsplit(":", 1)[-1], tree)
        t0 = time.time()
        try:
            meta = _build_squashfs(env_id, env_row, adapter, rel, modules,
                                   modules_init, caps, pack_tools,
                                   weft.store.emit, staging_rel=staging_rel)
        except WeftError as e:
            # publishes have no realization row — leave a durable trace
            weft.store.emit("env.publish.failed", env_id=env_id, site=site,
                            tree=tree, name=name, version=version,
                            error=e.code, detail=e.detail[:300])
            raise
        # a publisher-owned live mount at the published path would make
        # the mountpoint EACCES for every other user (FUSE without
        # allow_other hides mounts even from root — found by the chown
        # in the cross-user test): spot-check in a throwaway namespace
        # where possible, and never leave a mount behind either way
        wrap = "unshare -rm" if squashfs_mode(caps) == "userns" else ""
        _spot_check_and_mark(env_id, env_row, adapter, rel, "squashfs",
                             extra=meta, wrap=wrap,
                             verify_block=(weft.store.get_spec(
                                 env_row["spec_hash"]) or {}).get("verify"))
        if not wrap:
            _ensure_unmounted(adapter, f"{adapter.path(rel)}/mnt")
        meta["build_s"] = round(time.time() - t0, 1)
        if staging_why and "staging" not in meta:
            # honest numbers: say WHERE the churn landed and why
            meta["staging"] = {"used": False, "why": staging_why}

    # lock sidecar: everything a consumer needs to adopt WITHOUT solving
    lock_rel = f"{tree}/locks/{env_id.rsplit(':', 1)[-1]}.json"
    adapter.write_file(lock_rel, json.dumps({
        "env_id": env_id, "spec_hash": env_row["spec_hash"],
        "canonical": env_row["canonical"],
        "native_lock": env_row["native_lock"],
        "manifest": env_row["manifest"],
        "platforms": env_row["platforms"],
    }).encode())

    catalog = _read_catalog(adapter, tree)
    entry = catalog["envs"].setdefault(name, {"versions": {}, "latest": None})
    # grade + spec_summary are facts of the ARTIFACT, recorded at publish
    # time — computing them at read would mean fetching lock sidecars per
    # row. Republishing a version rewrites its entry, healing older rows.
    from .grade import grade_env
    spec = weft.store.get_spec(env_row["spec_hash"]) or {}
    entry["versions"][version] = {
        "env_id": env_id, "published_at": time.time(),
        "image_sha256": meta.get("image_sha256"),
        "image_bytes": meta.get("image_bytes"),
        # portability floor: shared trees meet heterogeneous clusters;
        # a consumer on an older-glibc machine deserves the warning at
        # ADOPT time, not a loader crash mid-job
        "glibc_floor": _glibc_floor(env_row["native_lock"]),
        "grade": grade_env(env_row["canonical"])["grade"],
        "spec_summary": {
            "spec_name": spec.get("name"),
            "platforms": env_row["platforms"],
            "packages_per_platform": {
                p: len(pkgs) for p, pkgs
                in env_row["canonical"]["platforms"].items()},
            "deps": spec.get("deps") or {},
        },
        "notes": notes,
    }
    if latest or not entry.get("latest"):
        # variant publishes (e.g. an old-glibc build of the same release)
        # pass latest=False so they don't hijack the default pointer
        entry["latest"] = version
    _write_catalog(adapter, tree, catalog)

    weft.store.audit_log("user", "env.publish", site=site,
                         command=f"{name}@{version} -> {env_id}",
                         result=tree)
    weft.store.emit("env.published", env_id=env_id, site=site, tree=tree,
                    name=name, version=version,
                    image_bytes=meta.get("image_bytes"))
    return {"env_id": env_id, "site": site, "tree": tree, "name": name,
            "version": version, **meta,
            "consumers": "register sites with ro_roots including this "
                         "tree, then env_adopt(site, tree, name)"}


def adopt(weft, site: str, tree: str, name: str,
          version: str = "latest") -> dict:
    """Resolve name→EnvID from the tree's catalog and import the env row
    from the stored lock — NO solving, no index access. The realization
    itself is adopted read-only in place on first use (ro_roots)."""
    adapter, tree = _validate(weft, site, tree, name, version)
    catalog = _read_catalog(adapter, tree)
    entry = (catalog.get("envs") or {}).get(name)
    if not entry or not entry.get("versions"):
        raise WeftError(
            "data.missing", f"nothing published as {name!r} in {tree}",
            stage="infra",
            hints={"published": sorted((catalog.get("envs") or {}))})
    v = entry.get("latest") if version == "latest" else version
    rec = entry["versions"].get(v)
    if not rec:
        raise WeftError(
            "data.missing", f"{name!r} has no version {version!r}",
            stage="infra",
            hints={"versions": sorted(entry["versions"]),
                   "latest": entry.get("latest")})
    env_id = rec["env_id"]
    if not weft.store.get_env(env_id):
        lock_rel = f"{tree}/locks/{env_id.rsplit(':', 1)[-1]}.json"
        side = json.loads(adapter.read_file(lock_rel).decode())
        weft.store.put_env(env_id, side["spec_hash"], side["canonical"],
                           side["native_lock"], side["manifest"],
                           side["platforms"])
    out = {"env_id": env_id, "name": name, "version": v, "tree": tree,
           "note": "imported from the published lock — no solve; use this "
                   "env_id in task_submit/kernel_start; extends_env works "
                   "on top"}
    floor = rec.get("glibc_floor")
    site_glibc = ((weft.store.get_site(site) or {})
                  .get("capabilities") or {}).get("glibc") or ""
    try:
        if floor and site_glibc and \
                tuple(int(x) for x in site_glibc.split(".")) \
                < tuple(int(x) for x in floor.split(".")):
            out["warning"] = (
                f"this env needs glibc >= {floor}; {site!r} has "
                f"{site_glibc} — its binaries will NOT run here. "
                f"Publish a version solved with system_requirements "
                f"{{'libc': '{site_glibc}'}} for this site")
    except ValueError:
        pass
    ro = ((weft.store.get_site(site) or {}).get("config") or {}) \
        .get("ro_roots") or []
    if tree not in [r.rstrip("/") for r in ro]:
        out["warning"] = (out.get("warning", "") + " | " if "warning" in out
                          else "") + \
                         (f"site {site!r} does not list {tree} in ro_roots "
                          f"— adoption-in-place will not trigger; "
                          f"re-register the site with ro_roots=[...,{tree!r}]")
    weft.store.emit("env.adopted", env_id=env_id, site=site, tree=tree,
                    name=name, version=v)
    return out


def unpublish(weft, site: str, tree: str, name: str, version: str,
              purge: bool = False) -> dict:
    """Remove the catalog pointer (new adoptions stop); the directory
    STAYS for the grace period unless purge=True. Running jobs survive a
    purge on nodes where the image is mounted (open fd), but consumers'
    integrity fences will fail loudly on next use — that is the design."""
    adapter, tree = _validate(weft, site, tree, name, version)
    catalog = _read_catalog(adapter, tree)
    entry = (catalog.get("envs") or {}).get(name)
    if not entry or version not in (entry.get("versions") or {}):
        raise WeftError("data.missing",
                        f"{name!r}@{version!r} is not in the catalog",
                        stage="infra")
    rec = entry["versions"].pop(version)
    if entry.get("latest") == version:
        entry["latest"] = max(entry["versions"],
                              key=lambda k: entry["versions"][k]
                              ["published_at"]) if entry["versions"] else None
    if not entry["versions"]:
        del catalog["envs"][name]
    _write_catalog(adapter, tree, catalog)
    out = {"name": name, "version": version, "env_id": rec["env_id"],
           "state": "unpublished",
           "note": "directory retained for the grace period; purge=True "
                   "deletes it"}
    if purge:
        rel = f"{tree}/envs/{rec['env_id'].rsplit(':', 1)[-1]}"
        mnt = shlex.quote(f"{rel}/mnt")
        adapter.run_cmd(f"fusermount -uz {mnt} 2>/dev/null; "
                        f"fusermount3 -uz {mnt} 2>/dev/null; "
                        f"rm -rf {shlex.quote(rel)} "
                        f"{shlex.quote(tree)}/locks/"
                        f"{rec['env_id'].rsplit(':', 1)[-1]}.json; true")
        out["state"] = "purged"
    weft.store.audit_log("user", "env.unpublish", site=site,
                         command=f"{name}@{version}",
                         result=out["state"])
    weft.store.emit("env.unpublished", env_id=rec["env_id"], site=site,
                    tree=tree, name=name, version=version,
                    purged=bool(purge))
    return out


def published(weft, site: str, tree: str) -> dict:
    """The tree's catalog as consumers see it, enriched per version with
    the read-time facts a host UI needs to render rows directly:
    `is_latest`, `runnable_here` (site glibc vs the recorded floor; None
    when the site's glibc is unknown — unknown ≠ runnable), `state_here`
    ({adopted-ro, ready, building, failed, missing} from this workspace's
    realization rows), and `last_used`. Write-time facts (grade,
    spec_summary, glibc_floor, image bytes) ride in the catalog itself."""
    adapter = weft.adapters.get(site)
    if adapter is None:
        raise WeftError("task.invalid", f"unknown site: {site}",
                        stage="infra",
                        hints={"registered": sorted(weft.adapters)})
    catalog = _read_catalog(adapter, tree)
    site_glibc = ((weft.store.get_site(site) or {})
                  .get("capabilities") or {}).get("glibc")
    for entry in (catalog.get("envs") or {}).values():
        for ver, rec in (entry.get("versions") or {}).items():
            rec["is_latest"] = entry.get("latest") == ver
            floor = rec.get("glibc_floor")
            runnable = None
            try:
                if not floor:
                    # no glibc requirement in the lock = no constraint
                    runnable = True if site_glibc else None
                elif site_glibc:
                    runnable = tuple(int(x) for x in site_glibc.split(".")) \
                        >= tuple(int(x) for x in floor.split("."))
            except ValueError:
                pass
            rec["runnable_here"] = runnable
            r = weft.store.get_realization(rec["env_id"], site)
            if r is None or r["state"] == "missing":
                rec["state_here"] = "missing"
            elif r["state"] == "ready":
                rec["state_here"] = "adopted-ro" if r["read_only"] \
                    else "ready"
            else:
                rec["state_here"] = r["state"]   # building / failed
            if r:
                rec["last_used"] = r["last_used"]
    return {"schema": "published:v1", "tree": tree, "site": site, **catalog}
