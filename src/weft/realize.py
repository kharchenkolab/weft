"""Realizers: make an EnvID usable on a site (doc 03 §4).

Phase 0/1 implements `prefix` (pixi install --frozen from the locked
manifest). Builds are fenced with a `.weft-ready` marker written only after
a successful spot-check: a re-run either finds the marker or redoes the
build — pixi environments are not relocatable, so build-in-place + marker
replaces the temp-dir + rename idiom.

Concurrency: one build per (EnvID, site); later requesters wait on the
same in-process future.
"""

from __future__ import annotations

import re
import shlex
import threading
import uuid as _uuid
from pathlib import Path

from .adapters.base import SiteAdapter
from .errors import WeftError
from .ids import ENVID_SCHEME
from .store import Store

_HEX64 = re.compile(r"[0-9a-f]{64}")

_BUILD_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_BUILD_LOCKS_GUARD = threading.Lock()


def env_dir_rel(env_id: str) -> str:
    # strip any scheme (env:v1:, env:v2:, ...) — colons don't belong in paths
    return f"envs/{env_id.rsplit(':', 1)[-1]}"


def _build_lock(env_id: str, site: str) -> threading.Lock:
    with _BUILD_LOCKS_GUARD:
        return _BUILD_LOCKS.setdefault((env_id, site), threading.Lock())


def _has_package(canonical: dict, name: str) -> bool:
    return any(
        p["name"] == name
        for plat in canonical.get("platforms", {}).values()
        for p in plat
    )


def module_prelude(modules: list[str], modules_init: str = "") -> str:
    """Shell prelude for site-module loads. Non-interactive shells often
    lack the `module` function; source the standard init scripts first.
    `modules_init` is a site-config snippet for quirks (e.g. MODULEPATH)."""
    lines = []
    if modules_init:
        lines.append(modules_init)
    lines.append(
        "if ! type module >/dev/null 2>&1; then\n"
        "  [ -f /usr/share/modules/init/sh ] && . /usr/share/modules/init/sh\n"
        "  [ -f /usr/share/lmod/lmod/init/sh ] && . /usr/share/lmod/lmod/init/sh\n"
        "  [ -f /etc/profile.d/modules.sh ] && . /etc/profile.d/modules.sh\n"
        "fi"
    )
    for m in modules:
        lines.append(
            f"module load {shlex.quote(m)} || "
            f"{{ echo 'weft: module load {m} failed' >&2; exit 90; }}"
        )
        # Tcl Environment Modules 3.x prints errors to stderr and exits
        # ZERO — the || above is inert there, and the job would run
        # against host toolchains with the env's name on the manifest.
        # Demand the load PRODUCT: the module (or a versioned expansion
        # of it) listed as loaded. Switch order is `module -t list`
        # (Lmod 8.1 parses a trailing -t as a MATCH PATTERN — clip
        # reality find); grep -i because sites re-case names (clip
        # lists gcc for GCC).
        pat = shlex.quote(f"^{m}(/|$)")
        lines.append(
            f"module -t list 2>&1 | grep -iEq {pat} || "
            f"{{ echo 'weft: module load {m} left no load product "
            f"(silent Tcl-EM failure?)' >&2; exit 90; }}"
        )
    return "\n".join(lines)


def ensure_realization(
    env_id: str, env_row: dict, adapter: SiteAdapter, store: Store,
    *, caps: dict | None = None, site_config: dict | None = None,
    prefer: str | None = None, pack_tools: dict | None = None,
) -> dict:
    """Capability-driven strategy selection + idempotent build (doc 03 §4).

    Cache-hit fast path; builds run under a per-(EnvID, site) lock. A
    marker on site with no store row is re-adopted (crash recovery); a
    store row with no marker is demoted and rebuilt (scratch purge).
    """
    from .strategy import select_strategy

    from .capability import compute_view
    libc = compute_view(caps or {}).get("glibc", "")
    if libc == "musl":
        raise WeftError(
            "env.unsatisfiable_on_site",
            "site libc is musl; conda-forge linux-64 packages need glibc",
            stage="realize",
            hints={
                "site": adapter.name,
                "suggestion": "run env-less tasks here, or use a glibc site "
                              "for anything needing a realized environment",
            },
        )
    site_plat = _site_platform(caps)
    lock_plats = list(env_row["canonical"].get("platforms") or {})
    if lock_plats and site_plat not in lock_plats:
        raise WeftError(
            "env.platform_mismatch",
            f"env is locked for {lock_plats} but site {adapter.name} "
            f"is {site_plat}",
            stage="realize",
            hints={
                "site": adapter.name,
                "site_platform": site_plat,
                "locked_platforms": lock_plats,
                "suggestion": "add the site's platform to the spec's "
                              "'platforms' and env_ensure again (platform "
                              "membership is identity: this yields a new "
                              "EnvID, solved for both)",
            },
        )
    # solvers that compile on site key their build caches on the platform
    pack_tools = {**(pack_tools or {}), "site_platform": site_plat}

    extras = env_row["canonical"]["extras"]
    modules = extras.get("modules") or []
    strategy = select_strategy(
        caps or {"internet": True, "runtimes": {}},
        modules=modules,
        container_base=extras.get("container_base"),
        prefer=prefer,
    )
    if strategy.endswith("container"):
        # v1: container realization not yet implemented — packed delivers
        # the same "no site network needed" property (documented deviation)
        store.emit("realize.fallback", env_id=env_id, site=adapter.name,
                   requested="container", using="packed")
        strategy = strategy.replace("container", "packed")

    # overlay: reuse an already-realized parent's prefix and materialize only
    # the delta (O(delta) instead of O(env)). Falls back on any doubt — to
    # whatever strategy capabilities selected, which is correct, just slower.
    base_strategy = strategy
    overlay_parent, overlay_parent_loc = (
        _overlay_parent(env_row, adapter, store, caps) or (None, None))
    if overlay_parent:
        strategy = "overlay"

    rel = env_dir_rel(env_id)
    with _build_lock(env_id, adapter.name):
        existing = store.get_realization(env_id, adapter.name)
        if existing and existing["state"] == "ready":
            # the recorded location, not the computed one: a read-only-root
            # adoption lives OUTSIDE the writable site root
            loc = existing["location"] or rel
            if adapter.file_exists(f"{loc}/.weft-ready"):
                store.touch_realization(env_id, adapter.name)  # LRU recency
                # integrity fence: a tampered env (deleted tool, partial
                # purge) must rebuild, not silently fall through to host
                # binaries with the locked env's name on the manifest
                marker = _marker(adapter, loc)
                if marker.get("parent"):
                    # a hot overlay keeps its parent hot: GC recency must
                    # never see a load-bearing parent as idle
                    store.touch_realization(marker["parent"], adapter.name)
                recorded = marker.get("bin_digest")
                marked_strategy = marker.get("strategy", strategy)
                ok = _fence_ok(recorded, lambda: _bin_digest(
                    adapter, loc, marked_strategy))
                # two-deep: an overlay is only as intact as its parent
                if ok and marker.get("parent"):
                    p_loc = marker.get("parent_location") \
                        or env_dir_rel(marker["parent"])
                    p_strategy = _marker(adapter, p_loc).get("strategy") \
                        or "prefix"
                    ok = (adapter.file_exists(f"{p_loc}/.weft-ready")
                          and _fence_ok(marker.get("parent_bin_digest"),
                                        lambda: _bin_digest(
                                            adapter, p_loc, p_strategy)))
                    if not ok:
                        store.emit("realize.parent_changed", env_id=env_id,
                                   site=adapter.name, parent=marker["parent"])
                if ok:
                    return existing
                if existing.get("read_only"):
                    # verify-and-REPORT: the caller cannot rebuild what it
                    # does not own — name the env and fall through to a
                    # build in the writable root (or fail, per policy)
                    _ro_integrity_failed(env_id, existing, adapter, store,
                                         site_config)
                else:
                    store.emit("realize.integrity_failed", env_id=env_id,
                               site=adapter.name,
                               note="executable inventory changed (here or "
                                    "in the parent); rebuilding")
            # site-side deletion (e.g. scratch purge): demote and rebuild
            store.set_realization(env_id, adapter.name, strategy, rel, "missing")
            # A BROKEN copy must not outrank an intact RO pack: the
            # writable-first guard protects a DELIBERATE rebuild (ready +
            # intact, returned above), not stale litter. If a read-only
            # root carries a verified copy, adopt it and displace the
            # carcass off the critical path (rename + background unlink)
            # instead of paying a rebuild — and a parallel-FS rm — here.
            adopted = _adopt_from_ro_roots(env_id, env_row, adapter, store,
                                           site_config, caps=caps)
            if adopted:
                _wipe_aside(adapter, rel, recreate=False)
                store.emit("realize.adopted", env_id=env_id,
                           site=adapter.name, via="ro-over-stale",
                           displaced=rel)
                return adopted
        elif adapter.file_exists(f"{rel}/.weft-ready"):
            # site has it but store forgot (crash recovery, or another
            # workspace/user built it): re-adopt — with the strategy the
            # marker RECORDS, not the one we would have picked: an overlay
            # built elsewhere must stay an overlay in our books, or the
            # parent-eviction guard cannot see the dependency
            store.set_realization(env_id, adapter.name,
                                  _marker(adapter, rel).get("strategy")
                                  or strategy, rel, "ready")
            # adoption IS a use: recency starts now, or LRU sees a
            # load-bearing env as never-used until its second job
            store.touch_realization(env_id, adapter.name)
            store.emit("realize.adopted", env_id=env_id, site=adapter.name,
                       via="marker")
            return store.get_realization(env_id, adapter.name)
        else:
            # read-only roots (admin/service-owned base envs): adopt in
            # place, never write or lease there. WRITABLE-FIRST precedence
            # (we only reach here on a writable miss): local state must
            # beat foreign state, or a user who rebuilt after a broken RO
            # copy would be stuck re-adopting the broken one.
            adopted = _adopt_from_ro_roots(env_id, env_row, adapter, store,
                                           site_config, caps=caps)
            if adopted:
                return adopted

        # shared roots (multiple users, one filesystem): in-process locks
        # don't reach across users — take a site-side lease around the build
        lease = _SiteLease(adapter, rel) \
            if (site_config or {}).get("shared") else None
        if lease is not None:
            if lease.acquire_or_adopt():
                # another user finished the build while we waited: adopt
                store.set_realization(env_id, adapter.name,
                                      _marker(adapter, rel).get("strategy")
                                      or strategy, rel, "ready")
                store.touch_realization(env_id, adapter.name)
                store.emit("realize.adopted", env_id=env_id,
                           site=adapter.name, via="shared-lease")
                return store.get_realization(env_id, adapter.name)
        store.set_realization(env_id, adapter.name, strategy, rel, "building")
        modules_init = (site_config or {}).get("modules_init", "")
        # an archived env rebuilds from the controller's blob — no site
        # network, even for a `prefix`-strategy site (eviction with
        # archive=True is the air-gapped reclamation path). An eligible
        # overlay still wins: it is cheaper than shipping the blob.
        archive_ref = _archived_ref(store, env_id,
                                    platform=_site_platform(caps))
        if archive_ref and strategy != "overlay" \
                and not strategy.endswith("packed"):
            strategy = ("modules+packed" if modules else "packed")
            store.set_realization(env_id, adapter.name, strategy, rel,
                                  "building")
        try:
            from .capability import squashfs_mode
            ns_ok = squashfs_mode(caps or {}) == "userns"
            if strategy == "overlay":
                try:
                    _build_overlay(env_id, env_row, adapter, rel,
                                   overlay_parent, overlay_parent_loc,
                                   store, pack_tools or {}, ns_wrap=ns_ok)
                except WeftError as e:
                    # composition didn't verify (ABI, shadowing, a build that
                    # needed something the parent lacks): the user still gets a
                    # working env — we just pay for a full build and say so.
                    store.emit("realize.overlay_fallback", env_id=env_id,
                               site=adapter.name, parent=overlay_parent,
                               reason=e.detail[:300])
                    strategy = base_strategy
                    if archive_ref and not strategy.endswith("packed"):
                        strategy = "modules+packed" if modules else "packed"
                    overlay_parent = None
                    store.set_realization(env_id, adapter.name, strategy, rel,
                                          "building")
            marker_extra: dict | None = None
            if strategy != "overlay":
                if strategy.endswith("squashfs"):
                    # layers + post_install happen INSIDE the image build
                    marker_extra = _build_squashfs(
                        env_id, env_row, adapter, rel, modules,
                        modules_init, caps, pack_tools or {}, store.emit)
                elif strategy.endswith("packed"):
                    _build_packed(env_id, env_row, adapter, rel, modules,
                                  modules_init, caps, pack_tools or {})
                else:
                    _build_prefix(env_id, env_row, adapter, rel, modules,
                                  modules_init)
                if not strategy.endswith("squashfs"):
                    _realize_layers(env_id, env_row, adapter, rel,
                                    (pack_tools or {}).get("solvers") or {},
                                    store.emit,
                                    offline=strategy.endswith("packed"),
                                    pack_tools=pack_tools)
                    # overlays skip post_install by construction: eligibility
                    # requires the child's steps to equal the parent's, whose
                    # prefix (sourced first) already carries their products
                    _stage_post_install_inputs(env_row, adapter, rel,
                                               pack_tools or {})
                    _run_post_install(env_row, adapter, rel)
            # squashfs anywhere in the chain + userns available: the
            # spot-check mounts in a throwaway namespace (also the only
            # way when the mountpoint is admin-owned or on a parallel FS)
            wrap = ""
            if ns_ok and (strategy.endswith("squashfs") or (
                    strategy == "overlay" and overlay_parent and "squashfs"
                    in (_marker(adapter, overlay_parent_loc
                                or env_dir_rel(overlay_parent))
                        .get("strategy") or ""))):
                wrap = "unshare -rm"
            _spot_check_and_mark(env_id, env_row, adapter, rel, strategy,
                                 parent=overlay_parent,
                                 parent_loc=overlay_parent_loc,
                                 extra=marker_extra, wrap=wrap)
        except WeftError as e:
            store.set_realization(
                env_id, adapter.name, strategy, rel, "failed", log=e.detail
            )
            raise
        finally:
            if lease is not None:
                lease.release()
        store.set_realization(env_id, adapter.name, strategy, rel, "ready")
        store.touch_realization(env_id, adapter.name,
                                nbytes=_prefix_bytes(adapter, rel))
        return store.get_realization(env_id, adapter.name)


def _adopt_from_ro_roots(env_id: str, env_row: dict, adapter: SiteAdapter,
                         store: Store, site_config: dict | None,
                         caps: dict | None = None):
    """Institutional shape: base envs live in admin-owned, READ-ONLY roots.
    Adopt a ready realization in place — verify, record (absolute location
    + read_only), never write or lease there."""
    for ro in (site_config or {}).get("ro_roots") or []:
        loc = f"{ro.rstrip('/')}/envs/{env_id.rsplit(':', 1)[-1]}"
        if not adapter.file_exists(f"{loc}/.weft-ready"):
            continue
        marker = _marker(adapter, loc)
        recorded = marker.get("bin_digest")
        if not _fence_ok(recorded, lambda: _bin_digest(
                adapter, loc, marker.get("strategy", "prefix"))):
            store.emit("realize.ro_integrity_failed", env_id=env_id,
                       site=adapter.name, location=loc,
                       owner_action=f"the owner of {ro} must re-materialize "
                                    f"this env; skipping it")
            continue
        check = f". {shlex.quote(adapter.path(loc))}/activate.sh"
        if "squashfs" in (marker.get("strategy") or ""):
            from .capability import squashfs_mode
            if squashfs_mode(caps or {}) == "userns":
                # an admin-owned mountpoint refuses DIRECT fusermount
                # (write-permission rule) — the whole consumer story on
                # shared trees rides namespaces (cross-user test finding)
                check = _ns_wrap_cmd(check)
        spot = adapter.run_activated(check, timeout=120)
        if spot.rc != 0:
            store.emit("realize.ro_integrity_failed", env_id=env_id,
                       site=adapter.name, location=loc,
                       owner_action=f"activation broken; the owner of {ro} "
                                    "must repair it; skipping")
            continue
        store.set_realization(env_id, adapter.name,
                              marker.get("strategy") or "prefix",
                              loc, "ready", read_only=True)
        store.touch_realization(env_id, adapter.name)
        store.emit("realize.adopted", env_id=env_id, site=adapter.name,
                   via="ro-root", location=loc)
        return store.get_realization(env_id, adapter.name)
    return None


def _ro_integrity_failed(env_id: str, existing: dict, adapter: SiteAdapter,
                         store: Store, site_config: dict | None) -> None:
    """A read-only adoption failed its fence: the caller cannot rebuild
    what it does not own. Report with the owner's action; by default fall
    through to a writable build (policy ro_integrity: "fail" stops here)."""
    from .policy import site_policy
    root = existing["location"].rsplit("/envs/", 1)[0]
    store.emit("realize.ro_integrity_failed", env_id=env_id,
               site=adapter.name, location=existing["location"],
               owner_action=f"the owner of {root} must re-materialize this "
                            "env; building a private copy meanwhile")
    policy = site_policy(store.get_site(adapter.name) or {})
    if policy.get("ro_integrity") == "fail":
        raise WeftError(
            "env.realize_failed",
            f"read-only realization of {env_id} failed integrity and site "
            "policy forbids a private rebuild",
            stage="realize",
            hints={"location": existing["location"],
                   "owner_action": f"ask the owner of {root} to "
                                   "re-materialize the env",
                   "policy": "ro_integrity: fail"})


def _ns_wrap_cmd(script: str) -> str:
    """Run `script` inside a throwaway user+mount namespace: FUSE mounts
    made by activation land there (regardless of who owns the mountpoint)
    and vanish when the script exits.

    The INNER shell prefers bash, mirroring run_activated and the job
    runner: conda activate.d hooks contain bashisms (glib's gio
    completion uses process substitution), and el7's bash-4.2-as-sh
    refuses them in posix mode — cbe's 5.2 tolerates what clip's 4.2
    breaks on, so `sh` here is a cross-site trap."""
    inner = (f"if command -v bash >/dev/null 2>&1; then "
             f"exec bash -c {shlex.quote(script)}; "
             f"else exec sh -c {shlex.quote(script)}; fi")
    return f"unshare -rm sh -c {shlex.quote(inner)}"


def _bind_wrap_cmd(script: str, staging: str, mount: str) -> str:
    """Like _ns_wrap_cmd, but with `staging` bind-mounted at `mount`
    first: the command sees (and writes) the prefix at the path that
    gets baked into shebangs/RPATHs while the bytes land on fast
    storage. Unprivileged — the bind lives and dies inside the
    throwaway userns; realpath through a bind is the mount path, so
    nothing can leak the staging location into the artifact (a symlink
    could)."""
    inner = (f"if command -v bash >/dev/null 2>&1; then "
             f"exec bash -c {shlex.quote(script)}; "
             f"else exec sh -c {shlex.quote(script)}; fi")
    s, m = shlex.quote(staging), shlex.quote(mount)
    pre = (f"mount --make-rprivate / 2>/dev/null; "
           f"mount --bind {s} {m} 2>/dev/null || mount -o bind {s} {m} || "
           f"{{ echo 'weft: staging bind-mount failed in userns' >&2; "
           f"exit 97; }}; ")
    return f"unshare -rm sh -c {shlex.quote(pre + inner)}"


class _StagedBuild:
    """Adapter proxy for bind-staged squashfs builds (netfs trees, the
    weft-ui ask): every command runs inside a throwaway userns with the
    staging dir bind-mounted at the canonical mount path — so the paths
    baked into the prefix stay the published tree's — and every file
    helper is redirected so the BYTES land in staging. The slow tree
    never sees the small-file churn, only the finished image."""

    def __init__(self, adapter: SiteAdapter, mount: str, staging: str):
        self._a = adapter
        self._mount = mount.rstrip("/")
        self._staging = staging.rstrip("/")

    def _twin(self, p: str) -> str | None:
        if p == self._mount or p.startswith(self._mount + "/"):
            return self._staging + p[len(self._mount):]
        return None

    def _redirect(self, rel: str) -> str:
        return self._twin(self._a.path(rel)) or rel

    def run_cmd(self, script: str, *, timeout: float = 120.0):
        return self._a.run_cmd(
            _bind_wrap_cmd(script, self._staging, self._mount),
            timeout=timeout)

    def run_activated(self, script: str, *, timeout: float = 120.0):
        # the bind wrap's inner shell already prefers bash
        return self.run_cmd(script, timeout=timeout)

    def write_file(self, rel: str, data: bytes, mode: int = 0o644) -> None:
        return self._a.write_file(self._redirect(rel), data, mode=mode)

    def read_file(self, rel: str, max_bytes: int | None = None) -> bytes:
        return self._a.read_file(self._redirect(rel), max_bytes=max_bytes)

    def file_exists(self, rel: str) -> bool:
        return self._a.file_exists(self._redirect(rel))

    def shim(self, argv: list[str], *, timeout: float = 60.0):
        # adapters exec the shim OUTSIDE any namespace: rewrite path
        # arguments under the mount to their staging twins (the shim's
        # file placement bakes no paths, so this is transparent)
        return self._a.shim([self._twin(a) or a for a in argv],
                            timeout=timeout)

    def __getattr__(self, name):
        return getattr(self._a, name)


def _marker(adapter: SiteAdapter, rel: str) -> dict:
    """The .weft-ready marker's contents, {} if unreadable."""
    import json as _json
    try:
        return _json.loads(adapter.read_file(f"{rel}/.weft-ready").decode())
    except (ValueError, WeftError):
        return {}


def _prefix_bytes(adapter: SiteAdapter, rel: str) -> int | None:
    """Footprint of a realized prefix — the LRU/quota number a host policy
    needs. Best-effort: an unreadable du must never fail a build."""
    try:
        from .runner_util import du_apparent_bytes_cmd
        r = adapter.run_cmd(
            du_apparent_bytes_cmd(shlex.quote(adapter.path(rel))),
            timeout=120)
        return int(r.out.strip().split()[0]) if r.out.strip() else None
    except (WeftError, ValueError, IndexError):
        return None


def _virtual_pkg_overrides(env_row: dict) -> str:
    """GPU (and similar) envs solve with virtual packages the INSTALL
    point may lack: a CUDA env realizes on a driverless login node while
    its jobs run on GPU nodes. The spec's system_requirements — already
    honored at solve time — must be honored at realize time too, via
    conda's documented override variables."""
    try:
        import tomllib
        sysreq = tomllib.loads(env_row["manifest"]) \
            .get("system-requirements") or {}
    except Exception:
        return ""
    out = ""
    if sysreq.get("cuda"):
        out += f"CONDA_OVERRIDE_CUDA={shlex.quote(str(sysreq['cuda']))} "
    return out


def _wipe_aside(adapter: SiteAdapter, rel: str, *,
                recreate: bool = True) -> str | None:
    """Clear `rel` in O(1): rename(2) the tree to a `.trash-*` sibling
    (same dir → same filesystem) and unlink it in the BACKGROUND.

    A synchronous rm -rf of a ~10^5-file prefix takes minutes on a
    parallel FS (BeeGFS/Lustre/GPFS) — at the 120s run_cmd default it
    TimeoutExpired'd out of realize entirely (cbe field report), and
    even with a long timeout it gates the critical path for something
    the build never needed to wait on. The trash name is namespaced so
    gc_sweep reaps orphans if the detached rm dies with the node.
    Returns the trash rel, or None if nothing existed."""
    trash = f"{rel}.trash-{_uuid.uuid4().hex[:8]}"
    p, t = shlex.quote(adapter.path(rel)), shlex.quote(adapter.path(trash))
    script = (f"if [ -e {p} ]; then mv -f {p} {t} && echo moved; "
              f"else echo clean; fi"
              + (f" && mkdir -p {p}" if recreate else "")
              + f" && {{ [ ! -e {t} ] || (nohup rm -rf {t} >/dev/null 2>&1 &); }}")
    r = adapter.run_cmd(script, timeout=60)
    if r.rc != 0:
        raise WeftError(
            "env.realize_failed",
            f"could not clear {rel} for a fresh build (rename-aside failed)",
            stage="realize", retryable=True,
            hints={"detail": (r.err or r.out)[-300:], "op": "wipe-aside",
                   "path": rel})
    return trash if "moved" in r.out else None


def _build_prefix(
    env_id: str, env_row: dict, adapter: SiteAdapter, rel: str,
    modules: list[str], modules_init: str = "", fresh: bool = True,
) -> None:
    # fresh=False: the caller already made the wipe-or-resume decision
    # (squashfs resume preserves partial content — this rm was silently
    # repaying resumed source builds until it learned to stand down)
    if fresh:
        _wipe_aside(adapter, rel)
    adapter.write_file(f"{rel}/pixi.toml", env_row["manifest"].encode())
    adapter.write_file(f"{rel}/pixi.lock", env_row["native_lock"].encode())
    manifest_path = adapter.path(f"{rel}/pixi.toml")
    overrides = _virtual_pkg_overrides(env_row)
    build = adapter.run_cmd(
        overrides +
        f"{shlex.quote(adapter.pixi_bin)} install --frozen "
        f"--manifest-path {shlex.quote(manifest_path)} 2>&1",
        timeout=5400,   # published/institutional envs are 10-15 GB with
                        # CUDA stacks; login nodes are often small VMs
    )
    if build.rc != 0:
        raise WeftError(
            "env.realize_failed",
            f"pixi install failed on {adapter.name}",
            stage="realize",
            hints={
                "log_tail": build.out[-2000:],
                "retryable": "maybe — check for network or disk errors in log_tail",
                # the adaptive lever, where an agent will actually read it
                "if_the_world_moved": "if the recorded packages are simply "
                                      "gone from the index, env_revise(env_id) "
                                      "re-solves the same spec and reports the "
                                      "diff (or set site policy on_drift="
                                      "'revise' to do it automatically)",
            },
        )
    hook = adapter.run_cmd(
        overrides +
        f"{shlex.quote(adapter.pixi_bin)} shell-hook "
        f"--manifest-path {shlex.quote(manifest_path)}",
        timeout=120,
    )
    if hook.rc != 0:
        raise WeftError(
            "env.realize_failed", "pixi shell-hook failed", stage="realize",
            hints={"log_tail": hook.err[-1000:]},
        )
    activate = ""
    if modules:
        activate += module_prelude(modules, modules_init) + "\n"
    activate += hook.out
    adapter.write_file(f"{rel}/activate.sh", activate.encode())


def _realize_layers(env_id: str, env_row: dict, adapter: SiteAdapter,
                    rel: str, solvers: dict, emit,
                    offline: bool = False, pack_tools: dict | None = None) -> None:
    """Non-conda layers (cran, julia, …) install on top of the base env,
    each appending its activation lines. One progress event per layer —
    source builds can be slow and the agent should see where time goes.

    `offline` (air-gapped sites, design B2): the layer's packages are
    downloaded controller-side, shipped as a CAS blob, and installed with
    no network — symmetric to the conda `packed` strategy. Solvers that
    cannot pack say so with a structured reason."""
    import time as _t
    layers = env_row["canonical"].get("layers") or {}
    for eco, layer in sorted(layers.items()):
        solver = solvers.get(eco)
        if solver is None:
            raise WeftError(
                "env.realize_failed",
                f"env has a {eco!r} layer but no such solver is registered",
                stage="realize",
                hints={"registered": sorted(solvers),
                       "suggestion": "enable the solver on this controller"},
            )
        t0 = _t.time()
        emit("realize.layer", env_id=env_id, site=adapter.name,
             layer=eco, packages=len(layer.get("records", [])),
             offline=offline)
        if offline:
            packer = getattr(solver, "pack_layer", None)
            if packer is None:
                raise WeftError(
                    "env.unsatisfiable_on_site",
                    f"the {eco} layer cannot be delivered to a site without "
                    "network (no packing support)",
                    stage="realize",
                    hints={"layer": eco,
                           "suggestion": f"move these deps to conda-forge "
                                         f"equivalents, build them as a task, "
                                         f"or use a site with index access"},
                )
            lines = packer(layer, adapter, rel, pack_tools or {})
        else:
            lines = solver.realize_layer(layer, adapter, rel)
        if lines:
            current = adapter.read_file(f"{rel}/activate.sh").decode()
            adapter.write_file(f"{rel}/activate.sh",
                               (current + "\n" + lines + "\n").encode())
        emit("realize.layer.done", env_id=env_id, site=adapter.name,
             layer=eco, elapsed_s=round(_t.time() - t0, 1))


def _stage_post_install_inputs(env_row: dict, adapter: SiteAdapter, rel: str,
                               pack_tools: dict) -> None:
    """Materialize the escape hatch's sources INTO the env dir, so a
    post_install step depends on content hashes, not on the controller's
    filesystem (the live-agent eval's landmine: a `pip install ./pkg` env
    that could never be rebuilt elsewhere)."""
    inputs = env_row["canonical"]["extras"].get("post_install_inputs") or []
    if not inputs:
        return
    cas = pack_tools.get("cas")
    transfers = pack_tools.get("transfers", {})
    dataman = pack_tools.get("dataman")
    if cas is None or dataman is None:
        raise WeftError(
            "env.realize_failed",
            "post_install_inputs need the data plane (controller CAS)",
            stage="realize")
    from .task import Task
    t = Task.from_dict({
        "command": "true",
        "inputs": [{"ref": i["ref"], "mount_as": i["mount_as"]}
                   for i in inputs]})
    dataman.ensure_at([i["ref"] for i in inputs], adapter, transfers)
    plan = dataman.materialize_plan(t, site=adapter.name)
    if not plan:
        return
    adapter.write_file(f"{rel}/post-inputs.tsv", plan.encode())
    endpoint = adapter.transfer_endpoint()
    r = adapter.shim(
        ["materialize", "--cas", endpoint["cas_root"],
         "--dir", adapter.path(rel),
         "--plan", adapter.path(f"{rel}/post-inputs.tsv")], timeout=600)
    if r.rc != 0:
        raise WeftError(
            "env.realize_failed",
            "could not stage post_install_inputs into the env",
            stage="realize", hints={"detail": r.err[:300]})


def _run_post_install(env_row: dict, adapter: SiteAdapter, rel: str) -> None:
    """The escape hatch for what package channels can't express: bespoke
    installers, R packages from source/git, custom build flags. Runs inside
    the activated env, in the env dir, on the target site — so it can be
    hashed (it is, into the EnvID) but not content-pinned; specs using it
    are flagged weakly-reproducible."""
    for cmd in env_row["canonical"]["extras"].get("post_install") or []:
        r = adapter.run_activated(
            f"cd {shlex.quote(adapter.path(rel))} && . ./activate.sh && ( {cmd} )",
            timeout=3600,
        )
        if r.rc != 0:
            raise WeftError(
                "env.realize_failed",
                f"post_install command failed: {cmd[:120]}",
                stage="realize",
                hints={"command": cmd, "log_tail": (r.err or r.out)[-1500:],
                       "note": "post_install runs on the target site inside "
                               "the activated env — air-gapped sites cannot "
                               "fetch; pin sources (e.g. dated CRAN snapshot "
                               "repos, git commit hashes) for reproducibility"},
            )


def _archived_ref(store: Store, env_id: str,
                  platform: str | None = None) -> str | None:
    """Did someone evict this env with archive=True? Then the blob is the
    fastest (and, on air-gapped sites, the only) way back. Archives are
    platform-specific artifacts — never reuse one across platforms."""
    from .evict import ARCHIVE_META
    for row in store.datarefs_with_meta(ARCHIVE_META, env_id):
        blob_plat = (row.get("meta") or {}).get("platform")
        if platform and blob_plat and blob_plat != platform:
            continue
        return row["ref"]
    return None


class _SiteLease:
    """Atomic-mkdir lease on the site filesystem — the cross-USER build
    lock shared roots need (in-process locks only cover one controller).
    Stale leases (holder crashed) are taken over after STALE_MIN."""

    STALE_MIN = 30       # dir-mtime fallback (holders without a beat)
    WAIT_S = 2.0
    MAX_WAIT_S = 3600.0
    HB_S = 60.0          # holder heartbeat period
    HB_STALE_S = 900     # takeover when the beat is this old

    def __init__(self, adapter: SiteAdapter, rel: str):
        self.adapter = adapter
        self.rel = rel
        self.lease = adapter.path(f"{rel}.lease")
        self._hb_stop = None

    def _beat_once(self) -> None:
        # epoch CONTENT, not mtime: login nodes are NTP-tight with each
        # other; the FS server's mtime clock is not (2026-07 sweep S2)
        self.adapter.run_cmd(
            f"date +%s > {shlex.quote(self.lease)}/hb 2>/dev/null; true",
            timeout=30)

    def _start_hb(self) -> None:
        """Legitimate builds run HOURS (cran source builds ~20 min/pkg);
        a fixed 30-min staleness stole the lease from LIVE builders and
        double-built. The beat decouples takeover from build length."""
        self._beat_once()
        self._hb_stop = threading.Event()

        def beat():
            while not self._hb_stop.wait(self.HB_S):
                try:
                    self._beat_once()
                except Exception:
                    pass

        threading.Thread(target=beat, daemon=True).start()

    def acquire_or_adopt(self) -> bool:
        """Returns True if the env became ready while we waited (adopt it
        instead of building); False once we hold the lease and must build."""
        import time as _t
        deadline = _t.time() + self.MAX_WAIT_S
        parent = self.lease.rsplit("/", 1)[0]
        while True:
            # -p the PARENT only: the lease mkdir itself must stay atomic,
            # but a missing parent dir must read as "create it", not as
            # "someone else holds the lease" (a spin-until-timeout bug)
            r = self.adapter.run_cmd(
                f"mkdir -p {shlex.quote(parent)} 2>/dev/null; "
                f"mkdir {shlex.quote(self.lease)} 2>/dev/null && echo got "
                f"|| echo busy", timeout=30)
            if "got" in r.out:
                self._start_hb()
                return False
            # another user is building: did they finish?
            if self.adapter.file_exists(f"{self.rel}/.weft-ready"):
                return True
            stale = self.adapter.run_cmd(
                f"hb={shlex.quote(self.lease)}/hb; "
                f"if [ -f \"$hb\" ]; then "
                f"age=$(( $(date +%s) - $(cat \"$hb\" 2>/dev/null "
                f"|| echo 0) )); "
                f"[ \"$age\" -gt {int(self.HB_STALE_S)} ] "
                f"&& echo stale || echo live; "
                f"else find {shlex.quote(self.lease)} -maxdepth 0 "
                f"-mmin +{self.STALE_MIN} 2>/dev/null | grep -q . && "
                f"echo stale || echo live; fi", timeout=30)
            if "stale" in stale.out:
                self.adapter.run_cmd(f"rm -rf {shlex.quote(self.lease)}")
                continue
            if _t.time() > deadline:
                raise WeftError(
                    "state.conflict",
                    "another user's env build held the lease too long",
                    stage="realize",
                    hints={"lease": self.lease,
                           "suggestion": "inspect the shared root; remove "
                                         "the lease dir if the builder died"})
            _t.sleep(self.WAIT_S)

    def release(self) -> None:
        if self._hb_stop is not None:
            self._hb_stop.set()
        self.adapter.run_cmd(f"rm -rf {shlex.quote(self.lease)} 2>/dev/null; true")


def _overlay_parent(env_row: dict, adapter: SiteAdapter, store: Store,
                    caps: dict | None = None) -> str | None:
    """Is this env an overlay candidate ON THIS SITE right now?"""
    parent = env_row.get("parent_env_id")
    if not parent or not env_row.get("layerable"):
        return None
    from .capability import compute_view
    if not compute_view(caps or {}).get("internet", True):
        return None      # the delta installs from indexes, site-side —
                         # air-gapped sites go packed/archive instead
    p_real = store.get_realization(parent, adapter.name)
    if not p_real or p_real["state"] != "ready":
        return None      # parent not realized here: nothing to stack on
    if p_real["strategy"] not in ("prefix", "modules+prefix",
                                  "squashfs", "modules+squashfs"):
        return None      # depth 1 only, and only on a pixi-prefix layout:
                         # packed parents lay out env/ differently and the
                         # toolchain prelude / parent fence assume .pixi
                         # (a MOUNTED squashfs parent presents exactly the
                         # pixi layout, one level down at mnt/)
    if p_real["strategy"].endswith("squashfs"):
        from .capability import squashfs_mode
        if squashfs_mode(caps or {}) is None:
            return None  # can neither ns-wrap the build commands nor
                         # direct-mount the parent — full build instead
    parent_row = store.get_env(parent)
    pe = ((parent_row or {}).get("canonical") or {}).get("extras", {})
    ce = (env_row.get("canonical") or {}).get("extras", {})
    for key in ("modules", "post_install"):
        if (pe.get(key) or []) != (ce.get(key) or []):
            return None  # an extras delta is not materialized by layer
                         # composition — a full prefix realizes it correctly
    # the RECORDED location: a read-only-root parent (admin-owned base)
    # composes exactly like a local one — overlay only ever READS it
    p_loc = p_real["location"] or env_dir_rel(parent)
    if not adapter.file_exists(f"{p_loc}/.weft-ready"):
        return None
    return (parent, p_loc)


def _build_overlay(env_id: str, env_row: dict, adapter: SiteAdapter, rel: str,
                   parent_env_id: str, parent_loc: str | None,
                   store: Store, pack_tools: dict,
                   ns_wrap: bool = False) -> None:
    """Reuse the parent's prefix; materialize ONLY the delta into this env's
    own layer dirs; compose at runtime via each ecosystem's search path.
    The parent may live in a READ-ONLY root (admin-owned base): every
    parent access below is a read — never write into parent_rel."""
    import json as _json
    import time as _t
    from .overlay import classify_delta
    from .toolchain import build_env_prelude, ensure_toolchain

    parent_row = store.get_env(parent_env_id)
    parent_rel = parent_loc or env_dir_rel(parent_env_id)
    parent_dir = adapter.path(parent_rel)
    delta = classify_delta(parent_row["canonical"], env_row["canonical"])
    if not delta["layerable"]:
        raise WeftError("env.realize_failed",
                        f"not layerable: {delta['why']}", stage="realize")

    # a squashfs parent presents the pixi layout INSIDE its mount: outer
    # activate.sh handles modules + the lazy mount (what we SOURCE); the
    # filesystem paths the build needs (headers, rlib, rpath keys) live
    # one level down at mnt/. Ensure the mount is live for the build —
    # eligibility already required a persistent-mount-capable site.
    parent_is_squashfs = "squashfs" in (_marker(adapter, parent_rel)
                                        .get("strategy") or "")
    parent_layout_rel = f"{parent_rel}/mnt" if parent_is_squashfs \
        else parent_rel
    parent_layout_dir = adapter.path(parent_layout_rel)
    # ns_wrap: every parent-touching build command runs in its own
    # user+mount namespace — activation's lazy mount lands there. This is
    # REQUIRED for admin-owned published parents (direct fusermount
    # refuses foreign-owned mountpoints) and self-cleaning everywhere.
    wrap = _ns_wrap_cmd if (ns_wrap and parent_is_squashfs) else (lambda s: s)
    if parent_is_squashfs:
        m = adapter.run_activated(wrap(
            f". {shlex.quote(parent_dir)}/activate.sh >/dev/null 2>&1; "
            f"test -f {shlex.quote(parent_layout_dir)}/activate.sh"),
            timeout=120)
        if m.rc != 0:
            raise WeftError(
                "env.realize_failed",
                "could not mount the squashfs parent for the overlay build",
                stage="realize",
                hints={"parent": parent_env_id,
                       "log_tail": (m.err or m.out)[-500:]})

    t0 = _t.time()
    store.emit("realize.overlay", env_id=env_id, site=adapter.name,
               parent=parent_env_id, delta=delta)
    _wipe_aside(adapter, rel)

    # a source-built delta needs a compiler that is NOT in the env; weft
    # brings its own, on PATH for the build only. Language layers (R,
    # julia) get it eagerly — they routinely compile; pypi tries wheels
    # first and only summons the toolchain if a build actually fails.
    prelude = ""
    if delta.get("layers_added"):
        from .toolchain import toolchain_fingerprint
        tc = ensure_toolchain(adapter, adapter.pixi_bin)
        if tc:
            prelude = build_env_prelude(adapter, tc, parent_layout_dir)
            # the compile cache must key on what ACTUALLY builds and links:
            # the resolved toolchain (not its requested spec) and the parent
            # prefix path the artifacts carry in their rpath
            pack_tools = {**pack_tools,
                          "toolchain_fingerprint":
                              toolchain_fingerprint(adapter),
                          "parent_prefix": parent_layout_dir}
    if parent_is_squashfs:
        pack_tools = {**pack_tools, "parent_layout_dir": parent_layout_dir,
                      "wrap_cmd": wrap}

    activation = [f". {shlex.quote(parent_dir)}/activate.sh"]
    solvers = pack_tools.get("solvers") or {}
    for eco, names in (delta.get("layers_added") or {}).items():
        solver = solvers.get(eco)
        if solver is None or not hasattr(solver, "realize_overlay"):
            raise WeftError(
                "env.realize_failed",
                f"the {eco} solver cannot realize an overlay layer",
                stage="realize")
        layer = (env_row["canonical"].get("layers") or {})[eco]
        parent_layer = (parent_row["canonical"].get("layers") or {}).get(eco)
        activation.append(solver.realize_overlay(
            layer, parent_layer, names, adapter, rel, parent_rel, prelude,
            pack_tools, parent_env_id))

    if delta.get("pypi_added"):
        try:
            activation.append(_overlay_pypi(env_id, env_row, parent_row,
                                            adapter, rel, parent_dir,
                                            delta["pypi_added"], prelude,
                                            wrap=wrap))
        except WeftError:
            if prelude:
                raise           # a compiler was already on PATH: real failure
            tc = ensure_toolchain(adapter, adapter.pixi_bin)
            if not tc:
                raise
            activation.append(_overlay_pypi(
                env_id, env_row, parent_row, adapter, rel, parent_dir,
                delta["pypi_added"],
                build_env_prelude(adapter, tc, parent_layout_dir),
                wrap=wrap))

    adapter.write_file(f"{rel}/activate.sh",
                       ("\n".join(activation) + "\n").encode())
    _verify_overlay(env_row, parent_row, adapter, rel, delta, wrap=wrap)
    adapter.write_file(f"{rel}/.weft-overlay",
                       _json.dumps({"parent": parent_env_id,
                                    "delta": delta}).encode())
    store.emit("realize.overlay.done", env_id=env_id, site=adapter.name,
               parent=parent_env_id, elapsed_s=round(_t.time() - t0, 1))


def _verify_overlay(env_row: dict, parent_row: dict, adapter: SiteAdapter,
                    rel: str, delta: dict, wrap=None) -> None:
    """Composition check — the backstop that makes the overlay safe: load
    every delta package AND a sample of the parent's, in the composed env.
    Any failure aborts the overlay and we fall back to a full prefix."""
    checks = []
    for name in (delta.get("layers_added") or {}).get("cran", []):
        checks.append(f"Rscript -e 'library({name})'")
    for name in (delta.get("layers_added") or {}).get("julia", []):
        checks.append(f"julia -e 'using {name}'")
    py_delta = [n for n in delta.get("pypi_added", [])]
    if py_delta:
        # by DISTRIBUTION name, not a guessed import name (scikit-learn's
        # module is sklearn): metadata visibility proves the composed path
        names = ", ".join(f'"{n}"' for n in py_delta)
        checks.append(
            "python -c 'import importlib.metadata as m; "
            f"[m.version(n) for n in ({names},)]'")
    # shadowing check: the parent's own top-level packages must still load
    parent_layers = parent_row["canonical"].get("layers") or {}
    for name in (parent_layers.get("cran", {}).get("top_level") or [])[:3]:
        if "/" not in name:
            checks.append(f"Rscript -e 'library({name})'")
    if not checks:
        return
    script = (f". {shlex.quote(adapter.path(rel))}/activate.sh && "
              + " && ".join(checks))
    wrap = wrap or (lambda s: s)
    r = adapter.run_activated(wrap(script), timeout=600)
    if r.rc != 0:
        raise WeftError(
            "env.realize_failed",
            "overlay composition check failed (ABI mismatch, shadowing, or a "
            "missing native dependency)",
            stage="realize",
            hints={"log_tail": (r.err or r.out)[-1200:]})


def _overlay_pypi(env_id: str, env_row: dict, parent_row: dict,
                  adapter: SiteAdapter, rel: str, parent_dir: str,
                  added: list[str], prelude: str, wrap=None) -> str:
    """Install ONLY the delta wheels into the child's own site dir, at the
    exact versions the lock pinned, with no dependency resolution (the lock
    already did it) — then compose via PYTHONPATH/PATH."""
    recs = {p["name"]: p
            for plat in env_row["canonical"]["platforms"].values()
            for p in plat if p["kind"] == "pypi"}
    picked = [recs[n] for n in added if n in recs]
    # the lock knows the artifact hashes: pin them, so the overlay installs
    # the same bytes a full-prefix `pixi install --frozen` would
    req_lines = [f'{r["name"]}=={r["version"]}'
                 + (f' --hash=sha256:{r["sha256"]}' if r.get("sha256") else "")
                 for r in picked]
    all_hashed = all(r.get("sha256") for r in picked)
    adapter.write_file(f"{rel}/pylib-requirements.txt",
                       ("\n".join(req_lines) + "\n").encode())
    req = f"{adapter.path(rel)}/pylib-requirements.txt"
    pylib = f"{adapter.path(rel)}/pylib"

    _w = wrap or (lambda s: s)

    def install(flags: str):
        return adapter.run_activated(_w(
            prelude +
            f". {shlex.quote(parent_dir)}/activate.sh && "
            f"mkdir -p {shlex.quote(pylib)} && "
            f"python -m pip install --no-deps --no-input {flags} "
            f"--target {shlex.quote(pylib)} -r {shlex.quote(req)} 2>&1"),
            timeout=1800)

    r = install("--require-hashes" if all_hashed else "")
    if r.rc != 0 and all_hashed:
        # the canonical lock keeps ONE artifact hash per (name, version);
        # pip may prefer a different (equally locked-version) wheel. Retry
        # unhashed rather than paying a full-prefix rebuild — but say so.
        r = install("")
    if r.rc != 0:
        raise WeftError(
            "env.realize_failed",
            "could not install the pypi delta into the overlay layer",
            stage="realize", hints={"log_tail": (r.err or r.out)[-1200:]})
    # entry-point scripts: pip --target does not place them on PATH
    binw = f"{adapter.path(rel)}/bin"
    adapter.run_cmd(
        f"mkdir -p {shlex.quote(binw)} && "
        f"if [ -d {shlex.quote(pylib)}/bin ]; then "
        f"cp -a {shlex.quote(pylib)}/bin/* {shlex.quote(binw)}/ 2>/dev/null; "
        f"fi; true")
    return (f'export PYTHONPATH="{pylib}${{PYTHONPATH:+:$PYTHONPATH}}"\n'
            f'export PATH="{binw}:$PATH"')


def _bin_dir_rel(rel: str, strategy: str) -> str:
    if strategy == "overlay":
        return f"{rel}/bin"      # the child's own layer bin (may be empty)
    if strategy.endswith("squashfs"):
        return f"{rel}/mnt/.pixi/envs/default/bin"   # inside the mount
    return f"{rel}/env/bin" if strategy.endswith("packed") \
        else f"{rel}/.pixi/envs/default/bin"


def _bin_digest(adapter: SiteAdapter, rel: str, strategy: str) -> str:
    """Fingerprint of the env's executable inventory. Catches the silent-
    rot class of failure: a deleted tool would otherwise fall through to
    the host's PATH and produce a wrong-provenance result that *looks*
    clean (live-agent eval finding).

    squashfs realizations fence on the IMAGE file instead (existence +
    byte size — the content is immutable by construction and may not be
    mounted when the fence runs; full sha256 lives in the marker for
    env_repair-time verification)."""
    if strategy.endswith("squashfs"):
        img = adapter.path(f"{rel}/image.sqfs")
        r = adapter.run_cmd(
            f"wc -c < {shlex.quote(img)} 2>/dev/null | tr -d ' ' || echo none",
            timeout=60)
        n = r.out.strip()
        return f"sqfs:{n}" if n and n != "none" else "none"
    d = adapter.path(_bin_dir_rel(rel, strategy))
    r = adapter.run_cmd(
        f"test -d {shlex.quote(d)} && cd {shlex.quote(d)} && "
        f"find . -type f -o -type l | LC_ALL=C sort | "
        f"{{ command -v sha256sum >/dev/null 2>&1 && sha256sum "
        f"|| shasum -a 256; }} | cut -d' ' -f1 || echo unverifiable",
        timeout=60,
    )
    got = r.out.strip().split()[-1] if r.out.strip() else "unverifiable"
    # only a real digest is evidence; anything else (missing dir, no hash
    # tool, pipeline failure) must NEVER equal a recorded digest — the
    # darwin fence was "none"=="none" for its whole life (sweep #6)
    return got if _HEX64.fullmatch(got) else "unverifiable"


def _fence_ok(recorded: str | None, fresh_fn) -> bool:
    """Integrity-fence comparison, fail-closed: a recorded real digest
    must be REPRODUCED — a fresh 'unverifiable' fails. Recorded
    'none'/'unverifiable' carries no information (legacy disarmed fence,
    hashless site): pass rather than rebuild forever."""
    if not recorded or recorded in ("none", "unverifiable"):
        return True
    return recorded == fresh_fn()


def _spot_check_and_mark(
    env_id: str, env_row: dict, adapter: SiteAdapter, rel: str, strategy: str,
    parent: str | None = None, parent_loc: str | None = None,
    extra: dict | None = None, wrap: str = "",
) -> None:
    """Activation succeeds; interpreter runs if present; then fence-mark
    (with the bin inventory fingerprint for later integrity checks).
    `wrap` prefixes the check command (userns-only squashfs sites run it
    as `unshare -rm sh -c ...` so the mount lives in a throwaway ns)."""
    check = f". {shlex.quote(adapter.path(rel))}/activate.sh"
    if _has_package(env_row.get("canonical", {}), "python"):
        check += " && python -c 'import sys; sys.exit(0)'"
    if wrap:
        # bash-preferring inner shell — same reasons as _ns_wrap_cmd
        check = _ns_wrap_cmd(check)
    # cold FUSE mount + interpreter start over a parallel FS can be slow
    spot = adapter.run_activated(check, timeout=300)
    if spot.rc != 0:
        raise WeftError(
            "env.realize_failed",
            "realization spot-check failed (corrupt build?)",
            stage="realize",
            hints={"log_tail": (spot.err or spot.out)[-1000:], "retryable": True},
        )
    import json as _json
    marker = {"strategy": strategy, **(extra or {})}
    if parent:
        # the integrity fence goes two deep: if the PARENT is repaired,
        # evicted, or tampered with, this child must rebuild too. Record
        # WHERE the parent lives — it may be a read-only root, and the
        # fence must re-digest the same tree it fingerprinted (with the
        # parent's OWN strategy: a squashfs parent fences on its image)
        parent_rel = parent_loc or env_dir_rel(parent)
        p_strategy = _marker(adapter, parent_rel).get("strategy") or "prefix"
        marker["parent"] = parent
        marker["parent_location"] = parent_rel
        marker["parent_bin_digest"] = _bin_digest(adapter, parent_rel,
                                                  p_strategy)
        marker["bin_digest"] = _bin_digest(adapter, rel, "overlay")
    else:
        marker["bin_digest"] = _bin_digest(adapter, rel, strategy)
    adapter.write_file(f"{rel}/.weft-ready",
                       (_json.dumps(marker) + "\n").encode())


def _build_packed(
    env_id: str, env_row: dict, adapter: SiteAdapter, rel: str,
    modules: list[str], modules_init: str, caps: dict | None,
    pack_tools: dict, fresh: bool = True,
) -> None:
    """Pack locally (pixi-pack: locked packages + offline installer),
    ship the archive as an ordinary CAS blob, unpack-verify on site.
    No network is needed from the site at any point."""
    import subprocess
    import tempfile

    pixi_pack = pack_tools.get("pixi_pack")
    cas = pack_tools.get("cas")
    transfers = pack_tools.get("transfers", {})
    if not pixi_pack:
        # no sibling binary next to pixi: fetch the pinned release for
        # the controller's platform (cached once under site-tools)
        try:
            from .site_tools import fetch_tool
            from .spec import current_platform
            pixi_pack = str(fetch_tool("pixi-pack", current_platform()))
        except WeftError:
            pixi_pack = None
    if not pixi_pack or cas is None:
        raise WeftError(
            "env.realize_failed",
            "packed strategy needs the pixi-pack tool configured on the "
            "controller (Weft(pixi_pack=...))",
            stage="realize",
            hints={"suggestion": "install pixi-pack next to pixi and pass its path"},
        )
    # an archive from a previous eviction is already exactly this blob
    store = pack_tools.get("store")
    existing = _archived_ref(store, env_id) if store is not None else None
    if existing and cas.kind_of(existing) is not None:
        digest = existing.split(":")[-1]
        row = store.get_dataref(existing)
        info = type("Info", (), {"ref": existing,
                                 "bytes": (row or {}).get("bytes", 0),
                                 "plain_sha256": (row or {}).get("meta", {})
                                 .get("sha256_plain")})()
    else:
        plat = _site_platform(caps)
        with tempfile.TemporaryDirectory(prefix="weft-pack-") as td:
            tdp = Path(td)
            (tdp / "pixi.toml").write_text(env_row["manifest"])
            (tdp / "pixi.lock").write_text(env_row["native_lock"])
            out_tar = tdp / "environment.tar"
            proc = subprocess.run(
                [pixi_pack, "--environment", "default", "--platform", plat,
                 "--output-file", str(out_tar), str(tdp)],
                capture_output=True, text=True, timeout=1800,
            )
            if proc.returncode != 0 or not out_tar.exists():
                raise WeftError(
                    "env.realize_failed",
                    "pixi-pack failed on the controller",
                    stage="realize",
                    hints={"log_tail": (proc.stderr or proc.stdout)[-1500:]},
                )
            info = cas.register_file(out_tar)
        digest = info.ref.split(":")[-1]

    # ship the archive through the ordinary data plane (dedup for free)
    endpoint = adapter.transfer_endpoint()
    method = transfers.get(endpoint["method"])
    if method is None:
        raise WeftError(
            "data.transfer_failed",
            f"no transfer method {endpoint['method']!r} for packed delivery",
            stage="realize",
        )
    method.transfer([(digest, info.bytes)], cas, endpoint,
                    verify={digest: info.plain_sha256 or digest})

    site_tar = f"{endpoint['cas_root']}/{digest[:2]}/{digest}"
    dest = adapter.path(rel)
    # fresh=False: wipe-or-resume was decided upstream (squashfs resume)
    if fresh:
        _wipe_aside(adapter, rel)
    else:
        adapter.run_cmd(f"mkdir -p {shlex.quote(dest)}")
    unpack = adapter.run_cmd(
        f"cd {shlex.quote(dest)} && "
        f"{shlex.quote(adapter.path('bin/pixi-unpack'))} "
        f"--output-directory {shlex.quote(dest)} --shell bash "
        f"{shlex.quote(site_tar)}",
        timeout=1800,
    )
    if unpack.rc != 0:
        raise WeftError(
            "env.realize_failed",
            "pixi-unpack failed on site",
            stage="realize",
            hints={"log_tail": (unpack.err or unpack.out)[-1500:],
                   "retryable": True},
        )
    # pixi-unpack writes <dest>/env plus an activation script; wrap it so
    # the activation contract (modules first, then env) holds
    adapter.run_cmd(
        f"mv {shlex.quote(dest)}/activate.sh {shlex.quote(dest)}/activate.inner.sh"
    )
    activate = ""
    if modules:
        activate += module_prelude(modules, modules_init) + "\n"
    activate += f". {shlex.quote(dest)}/activate.inner.sh\n"
    adapter.write_file(f"{rel}/activate.sh", activate.encode())


def _build_squashfs(
    env_id: str, env_row: dict, adapter: SiteAdapter, rel: str,
    modules: list[str], modules_init: str, caps: dict | None,
    pack_tools: dict, emit, staging_rel: str | None = None,
) -> dict:
    """One mounted image instead of ~100k files on the shared FS.

    Layout (the realization dir stays a plain directory, so every marker/
    adoption/footprint path works unchanged):

        envs/<hash>/
          .weft-ready    marker (strategy, image_sha256, image_bytes, …)
          activate.sh    module prelude + lazy idempotent mount + source
          image.sqfs     the env, one zstd-compressed immutable object
          mnt/           mountpoint; the env tree appears here when mounted

    The CONTENT is built at mnt/ (a normal prefix build — or a packed
    unpack on air-gapped sites — plus layers and post_install), squashed,
    then the tree is deleted: the image is the realization. Mounting is
    lazy at activation (direct mode) or namespace-scoped per job (userns
    mode; the runner wraps the job — misc/sqaush.md, verified on
    cbe.next 2026-07-14).

    `staging_rel` decouples build STORAGE from build PATH (publish to a
    slow netfs tree): the prefix materializes in the staging dir, which
    every build command sees bind-mounted at mnt/ inside its userns —
    baked paths stay the tree's, the small-file churn stays on fast
    storage, and the tree receives one sequential image write. Gated by
    a live probe; a site where the bind cannot work falls back to
    building at the destination, with the reason emitted and returned.
    """
    from .capability import compute_view
    sq = compute_view(caps or {}).get("squashfs") or {}
    mk, fuse_mount = sq.get("mksquashfs"), sq.get("squashfuse")
    if not (mk and fuse_mount):
        raise WeftError(
            "env.unsatisfiable_on_site",
            "squashfs strategy needs mksquashfs and squashfuse on site (v1 "
            "builds images site-side)", stage="realize",
            hints={"squashfs": sq})
    inner = f"{rel}/mnt"
    internet = bool(compute_view(caps or {}).get("internet"))
    build = adapter          # what the content stages talk to
    content = inner          # where the bytes actually are
    staging_note = None
    ns_probed_ok = None      # live unshare probe result (staging block)
    if staging_rel:
        mount_abs, staging_abs = adapter.path(inner), \
            adapter.path(staging_rel)
        adapter.run_cmd(f"mkdir -p {shlex.quote(staging_abs)} "
                        f"{shlex.quote(mount_abs)}", timeout=300)
        # capabilities can be stale and container/kernel policy varies:
        # the gate is a live probe of the exact harness, not a belief
        probe = adapter.run_cmd(
            _bind_wrap_cmd("true", staging_abs, mount_abs), timeout=120)
        ns_probed_ok = probe.rc == 0
        if probe.rc == 0:
            build = _StagedBuild(adapter, mount_abs, staging_abs)
            content = staging_rel
            emit("realize.staged", env_id=env_id, site=adapter.name,
                 staging=staging_abs, mount=mount_abs)
        else:
            staging_note = ("bind probe failed: "
                            + ((probe.err or probe.out) or "")[-240:])
            emit("realize.staging_skipped", env_id=env_id,
                 site=adapter.name, reason=staging_note)
            staging_rel = None
    # resume, don't repay: a killed/failed build leaves content behind;
    # when its pixi.lock is byte-identical to THIS env's lock the content
    # can only be a partial build of the same identity — every stage
    # below is incremental (pixi revalidates, layer installers skip
    # what's installed), so retries converge instead of re-compiling the
    # world on flaky sites. Any mismatch → clean slate as before.
    # (post_install re-runs on resume — same doctrine as bundle import.)
    import hashlib as _hl
    want = _hl.sha256(env_row["native_lock"].encode()).hexdigest()
    have = ""
    if adapter.file_exists(f"{content}/pixi.lock"):
        r0 = adapter.run_cmd(
            f"sha256sum {shlex.quote(adapter.path(content))}/pixi.lock "
            f"2>/dev/null || shasum -a 256 "
            f"{shlex.quote(adapter.path(content))}/pixi.lock 2>/dev/null",
            timeout=300)
        if r0.rc != 0 or not (r0.out or "").strip():
            # unknown ≠ mismatch: a probe that CANNOT verify must never
            # translate into deleting an hour of build progress
            raise WeftError(
                "env.realize_failed",
                "existing build content found but its lock cannot be "
                "verified for resume", stage="realize", retryable=True,
                hints={"location": content,
                       "suggestion": "retry (transient fs/transport "
                                     "error), or remove the directory "
                                     "manually for a clean slate"})
        have = r0.out.split()[0]
    if have == want:
        emit("realize.resumed", env_id=env_id, site=adapter.name,
             location=content)
    else:
        # big trees on parallel filesystems take minutes to unlink —
        # rename them aside and let the delete run behind the build
        _wipe_aside(adapter, rel)
        if staging_rel:
            # both sides start clean; mnt/ comes back as the mountpoint
            _wipe_aside(adapter, staging_rel)
            adapter.run_cmd(f"mkdir -p {shlex.quote(adapter.path(inner))}")
    # content: modules stay OUT of the image (module preludes are host
    # state, run before activation — the outer script carries them)
    if internet:
        _build_prefix(env_id, env_row, build, inner, [], modules_init,
                      fresh=False)
    else:
        _build_packed(env_id, env_row, build, inner, [], modules_init,
                      caps, pack_tools, fresh=False)
    _realize_layers(env_id, env_row, build, inner,
                    pack_tools.get("solvers") or {}, emit,
                    offline=not internet, pack_tools=pack_tools)
    _stage_post_install_inputs(env_row, build, inner, pack_tools)
    _run_post_install(env_row, build, inner)

    img = adapter.path(f"{rel}/image.sqfs")
    t0 = __import__("time").time()
    emit("realize.squashfs", env_id=env_id, site=adapter.name)
    r = adapter.run_cmd(
        f"{shlex.quote(mk)} {shlex.quote(adapter.path(content))} "
        f"{shlex.quote(img)} -comp zstd -noappend -no-progress 2>&1 || "
        f"{shlex.quote(mk)} {shlex.quote(adapter.path(content))} "
        f"{shlex.quote(img)} -noappend -no-progress 2>&1",
        timeout=3600)
    if r.rc != 0:
        raise WeftError(
            "env.realize_failed", "mksquashfs failed on site",
            stage="realize", hints={"log_tail": (r.err or r.out)[-1500:]})
    meta = adapter.run_cmd(
        f"h=$(sha256sum {shlex.quote(img)} 2>/dev/null || "
        f"shasum -a 256 {shlex.quote(img)}); "
        f"printf '%s %s' \"${{h%% *}}\" \"$(wc -c < {shlex.quote(img)} | tr -d ' ')\"",
        timeout=600)
    parts = meta.out.strip().split()
    image_sha256 = parts[0] if parts else ""
    image_bytes = int(parts[1]) if len(parts) > 1 else 0
    # the tree was scaffolding; the image is the realization (unlinking
    # ~10^5 files on a parallel FS takes minutes — rename aside, delete
    # in the background, keep the build moving)
    _wipe_aside(adapter, content, recreate=False)
    adapter.run_cmd(f"mkdir -p {shlex.quote(adapter.path(inner))}")

    _write_squashfs_activation(adapter, rel, modules, modules_init)
    # tombstones in the mountpoint, then PROVE the mount still works
    # over them (fuse3 allows non-empty mountpoints; old fuse2 refuses —
    # there, strip and keep the env working, and say so)
    _write_mount_tombstones(adapter, rel)
    from .capability import squashfs_mode
    need_ns = squashfs_mode(caps or {}) == "userns"
    if need_ns and ns_probed_ok is False:
        # unshare demonstrably fails in THIS harness (the staging probe
        # said so) — the mount cannot be verified here. Keep the shims:
        # the activation's nonempty-first chain covers both fuse
        # generations (validated fuse2+fuse3); say what was not proven.
        emit("realize.tombstones_unverified", env_id=env_id,
             site=adapter.name,
             note="no usable namespace to probe the mount over the "
                  "legibility shims; kept on the strength of the "
                  "nonempty-first mount chain")
    else:
        check = (f". {shlex.quote(adapter.path(rel))}/activate.sh "
                 f">/dev/null 2>&1 && "
                 f"test -f {shlex.quote(adapter.path(inner))}/activate.sh")
        if need_ns:
            check = _ns_wrap_cmd(check)
        probe = adapter.run_activated(check, timeout=180)
        if probe.rc != 0:
            _strip_mount_tombstones(adapter, rel)
            emit("realize.tombstones_stripped", env_id=env_id,
                 site=adapter.name,
                 note="this site's FUSE refuses non-empty mountpoints "
                      "even with the fallback chain; bare-exec "
                      "legibility shims removed")
    emit("realize.squashfs.done", env_id=env_id, site=adapter.name,
         image_bytes=image_bytes,
         elapsed_s=round(__import__("time").time() - t0, 1))
    out = {"image_sha256": image_sha256, "image_bytes": image_bytes}
    if staging_rel:
        out["staging"] = {"used": True, "dir": adapter.path(staging_rel)}
    elif staging_note:
        out["staging"] = {"used": False, "why": staging_note}
    return out


_TOMBSTONE = """#!/bin/sh
echo "weft: $(basename "$0") lives inside this env's mount namespace and is not directly executable from outside." >&2
echo "weft: activation mounts the image first — use session_runtime()['exec_template'], session_exec, kernel_start, or task_submit." >&2
exit 127
"""


def _write_mount_tombstones(adapter: SiteAdapter, rel: str) -> None:
    """Legibility for the UNMOUNTED mountpoint: a bare exec of the
    in-mount interpreter path from outside the namespace hits ENOENT
    three directories deep, and consumers fall back to the wrong
    interpreter (field report). These shims sit AT those paths, name
    the lever, and exit 127; the mounted image shadows them completely.
    Callers must verify-and-strip: an old fuse2 refuses non-empty
    mountpoints, and a working env beats a legible corpse."""
    bin_rel = f"{rel}/mnt/.pixi/envs/default/bin"
    adapter.run_cmd(f"mkdir -p {shlex.quote(adapter.path(bin_rel))}",
                    timeout=60)
    for name in ("python", "python3", "R", "Rscript", "julia"):
        adapter.write_file(f"{bin_rel}/{name}", _TOMBSTONE.encode(),
                           mode=0o755)


def _strip_mount_tombstones(adapter: SiteAdapter, rel: str) -> None:
    adapter.run_cmd(
        f"rm -rf {shlex.quote(adapter.path(rel + '/mnt/.pixi'))} "
        f"2>/dev/null; true", timeout=60)


def _write_squashfs_activation(adapter: SiteAdapter, rel: str,
                               modules: list[str],
                               modules_init: str = "") -> None:
    """(Re)generate the OUTER activation sidecar. Idempotent and separate
    from the image build so a republish can heal sidecars — needed the
    first time a shared tree met a second cluster."""
    envdir = adapter.path(rel)
    activate = ""
    if modules:
        activate += module_prelude(modules, modules_init) + "\n"
    # the mounter is DISCOVERED at activation time, never baked: published
    # trees are shared across heterogeneous clusters (measured: two VBC
    # clusters mount the same /groups at the same paths but keep
    # squashfuse_ll in different places)
    activate += (
        f'__weft_sq="{envdir}"\n'
        "__weft_mount() {\n"
        "    for __c in squashfuse_ll squashfuse; do\n"
        "        for __p in \"$(command -v \"$__c\" 2>/dev/null)\" \\\n"
        "                   \"/usr/libexec/apptainer/bin/$__c\" \\\n"
        "                   \"/usr/bin/$__c\" \"/usr/sbin/$__c\"; do\n"
        "            [ -n \"$__p\" ] && [ -x \"$__p\" ] || continue\n"
        # -o nonempty FIRST: fuse2 refuses non-empty mountpoints (the
        # legibility tombstones live there) without it; fuse3 rejects
        # the option but allows non-empty by default — so try both ways
        "            \"$__p\" -o nonempty \"$1\" \"$2\" 2>/dev/null && return 0\n"
        "            \"$__p\" \"$1\" \"$2\" 2>/dev/null && return 0\n"
        "        done\n"
        "    done\n"
        "    return 1\n"
        "}\n"
        "if [ ! -f \"$__weft_sq/mnt/activate.sh\" ]; then\n"
        "    __weft_mount \"$__weft_sq/image.sqfs\" \"$__weft_sq/mnt\" || true\n"
        "fi\n"
        "if [ ! -f \"$__weft_sq/mnt/activate.sh\" ]; then\n"
        "    echo 'weft: squashfs env not mounted (no fuse/squashfuse "
        "here? run inside a job, or unshare -rm)' >&2\n"
        "    return 1 2>/dev/null || exit 1\n"
        "fi\n"
        ". \"$__weft_sq/mnt/activate.sh\"\n")
    adapter.write_file(f"{rel}/activate.sh", activate.encode())


def _site_platform(caps: dict | None) -> str:
    from .capability import compute_view
    view = compute_view(caps or {})
    osname = view.get("os", "linux")
    arch = view.get("arch", "x86_64")
    if osname == "darwin":
        return "osx-arm64" if arch in ("arm64", "aarch64") else "osx-64"
    return "linux-aarch64" if arch in ("arm64", "aarch64") else "linux-64"
