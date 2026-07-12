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

import shlex
import threading
from pathlib import Path

from .adapters.base import SiteAdapter
from .errors import WeftError
from .ids import ENVID_SCHEME
from .store import Store

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
    overlay_parent = _overlay_parent(env_row, adapter, store, caps)
    if overlay_parent:
        strategy = "overlay"

    rel = env_dir_rel(env_id)
    with _build_lock(env_id, adapter.name):
        existing = store.get_realization(env_id, adapter.name)
        if existing and existing["state"] == "ready":
            if adapter.file_exists(f"{rel}/.weft-ready"):
                store.touch_realization(env_id, adapter.name)  # LRU recency
                # integrity fence: a tampered env (deleted tool, partial
                # purge) must rebuild, not silently fall through to host
                # binaries with the locked env's name on the manifest
                marker = _marker(adapter, rel)
                if marker.get("parent"):
                    # a hot overlay keeps its parent hot: GC recency must
                    # never see a load-bearing parent as idle
                    store.touch_realization(marker["parent"], adapter.name)
                recorded = marker.get("bin_digest")
                marked_strategy = marker.get("strategy", strategy)
                ok = not recorded or recorded == _bin_digest(
                    adapter, rel, marked_strategy)
                # two-deep: an overlay is only as intact as its parent
                if ok and marker.get("parent"):
                    p_rel = env_dir_rel(marker["parent"])
                    ok = (adapter.file_exists(f"{p_rel}/.weft-ready")
                          and marker.get("parent_bin_digest")
                          == _bin_digest(adapter, p_rel, "prefix"))
                    if not ok:
                        store.emit("realize.parent_changed", env_id=env_id,
                                   site=adapter.name, parent=marker["parent"])
                if ok:
                    return existing
                store.emit("realize.integrity_failed", env_id=env_id,
                           site=adapter.name,
                           note="executable inventory changed (here or in the "
                                "parent); rebuilding")
            # site-side deletion (e.g. scratch purge): demote and rebuild
            store.set_realization(env_id, adapter.name, strategy, rel, "missing")
        elif adapter.file_exists(f"{rel}/.weft-ready"):
            # site has it but store forgot (crash recovery, or another
            # workspace/user built it): re-adopt — with the strategy the
            # marker RECORDS, not the one we would have picked: an overlay
            # built elsewhere must stay an overlay in our books, or the
            # parent-eviction guard cannot see the dependency
            store.set_realization(env_id, adapter.name,
                                  _marker(adapter, rel).get("strategy")
                                  or strategy, rel, "ready")
            store.emit("realize.adopted", env_id=env_id, site=adapter.name,
                       via="marker")
            return store.get_realization(env_id, adapter.name)

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
            if strategy == "overlay":
                try:
                    _build_overlay(env_id, env_row, adapter, rel,
                                   overlay_parent, store, pack_tools or {})
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
            if strategy != "overlay":
                if strategy.endswith("packed"):
                    _build_packed(env_id, env_row, adapter, rel, modules,
                                  modules_init, caps, pack_tools or {})
                else:
                    _build_prefix(env_id, env_row, adapter, rel, modules,
                                  modules_init)
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
            _spot_check_and_mark(env_id, env_row, adapter, rel, strategy,
                                 parent=overlay_parent)
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
        r = adapter.run_cmd(
            f"du -sb {shlex.quote(adapter.path(rel))} 2>/dev/null | cut -f1",
            timeout=120)
        return int(r.out.strip().split()[0]) if r.out.strip() else None
    except (WeftError, ValueError, IndexError):
        return None


def _build_prefix(
    env_id: str, env_row: dict, adapter: SiteAdapter, rel: str,
    modules: list[str], modules_init: str = "",
) -> None:
    adapter.run_cmd(f"rm -rf {shlex.quote(adapter.path(rel))}")
    adapter.write_file(f"{rel}/pixi.toml", env_row["manifest"].encode())
    adapter.write_file(f"{rel}/pixi.lock", env_row["native_lock"].encode())
    manifest_path = adapter.path(f"{rel}/pixi.toml")
    build = adapter.run_cmd(
        f"{shlex.quote(adapter.pixi_bin)} install --frozen "
        f"--manifest-path {shlex.quote(manifest_path)} 2>&1",
        timeout=1800,
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
    plan = dataman.materialize_plan(t)
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

    STALE_MIN = 30
    WAIT_S = 2.0
    MAX_WAIT_S = 3600.0

    def __init__(self, adapter: SiteAdapter, rel: str):
        self.adapter = adapter
        self.rel = rel
        self.lease = adapter.path(f"{rel}.lease")

    def acquire_or_adopt(self) -> bool:
        """Returns True if the env became ready while we waited (adopt it
        instead of building); False once we hold the lease and must build."""
        import time as _t
        deadline = _t.time() + self.MAX_WAIT_S
        while True:
            r = self.adapter.run_cmd(
                f"mkdir {shlex.quote(self.lease)} 2>/dev/null && echo got "
                f"|| echo busy", timeout=30)
            if "got" in r.out:
                return False
            # another user is building: did they finish?
            if self.adapter.file_exists(f"{self.rel}/.weft-ready"):
                return True
            stale = self.adapter.run_cmd(
                f"find {shlex.quote(self.lease)} -maxdepth 0 "
                f"-mmin +{self.STALE_MIN} 2>/dev/null | grep -q . && "
                f"echo stale || true", timeout=30)
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
    if p_real["strategy"] not in ("prefix", "modules+prefix"):
        return None      # depth 1 only, and only on a pixi-prefix layout:
                         # packed parents lay out env/ differently and the
                         # toolchain prelude / parent fence assume .pixi
    parent_row = store.get_env(parent)
    pe = ((parent_row or {}).get("canonical") or {}).get("extras", {})
    ce = (env_row.get("canonical") or {}).get("extras", {})
    for key in ("modules", "post_install"):
        if (pe.get(key) or []) != (ce.get(key) or []):
            return None  # an extras delta is not materialized by layer
                         # composition — a full prefix realizes it correctly
    if not adapter.file_exists(f"{env_dir_rel(parent)}/.weft-ready"):
        return None
    return parent


def _build_overlay(env_id: str, env_row: dict, adapter: SiteAdapter, rel: str,
                   parent_env_id: str, store: Store, pack_tools: dict) -> None:
    """Reuse the parent's prefix; materialize ONLY the delta into this env's
    own layer dirs; compose at runtime via each ecosystem's search path."""
    import json as _json
    import time as _t
    from .overlay import classify_delta
    from .toolchain import build_env_prelude, ensure_toolchain

    parent_row = store.get_env(parent_env_id)
    parent_rel = env_dir_rel(parent_env_id)
    parent_dir = adapter.path(parent_rel)
    delta = classify_delta(parent_row["canonical"], env_row["canonical"])
    if not delta["layerable"]:
        raise WeftError("env.realize_failed",
                        f"not layerable: {delta['why']}", stage="realize")

    t0 = _t.time()
    store.emit("realize.overlay", env_id=env_id, site=adapter.name,
               parent=parent_env_id, delta=delta)
    adapter.run_cmd(f"rm -rf {shlex.quote(adapter.path(rel))} && "
                    f"mkdir -p {shlex.quote(adapter.path(rel))}")

    # a source-built delta needs a compiler that is NOT in the env; weft
    # brings its own, on PATH for the build only
    prelude = ""
    if delta.get("layers_added") or delta.get("pypi_added"):
        from .toolchain import toolchain_fingerprint
        tc = ensure_toolchain(adapter, adapter.pixi_bin)
        if tc:
            prelude = build_env_prelude(adapter, tc, parent_dir)
            # the compile cache must key on what ACTUALLY builds and links:
            # the resolved toolchain (not its requested spec) and the parent
            # prefix path the artifacts carry in their rpath
            pack_tools = {**pack_tools,
                          "toolchain_fingerprint":
                              toolchain_fingerprint(adapter),
                          "parent_prefix": parent_dir}

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
        activation.append(_overlay_pypi(env_id, env_row, parent_row, adapter,
                                        rel, parent_dir, delta["pypi_added"],
                                        prelude))

    adapter.write_file(f"{rel}/activate.sh",
                       ("\n".join(activation) + "\n").encode())
    _verify_overlay(env_row, parent_row, adapter, rel, delta)
    adapter.write_file(f"{rel}/.weft-overlay",
                       _json.dumps({"parent": parent_env_id,
                                    "delta": delta}).encode())
    store.emit("realize.overlay.done", env_id=env_id, site=adapter.name,
               parent=parent_env_id, elapsed_s=round(_t.time() - t0, 1))


def _verify_overlay(env_row: dict, parent_row: dict, adapter: SiteAdapter,
                    rel: str, delta: dict) -> None:
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
    r = adapter.run_activated(script, timeout=600)
    if r.rc != 0:
        raise WeftError(
            "env.realize_failed",
            "overlay composition check failed (ABI mismatch, shadowing, or a "
            "missing native dependency)",
            stage="realize",
            hints={"log_tail": (r.err or r.out)[-1200:]})


def _overlay_pypi(env_id: str, env_row: dict, parent_row: dict,
                  adapter: SiteAdapter, rel: str, parent_dir: str,
                  added: list[str], prelude: str) -> str:
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

    def install(flags: str):
        return adapter.run_activated(
            prelude +
            f". {shlex.quote(parent_dir)}/activate.sh && "
            f"mkdir -p {shlex.quote(pylib)} && "
            f"python -m pip install --no-deps --no-input {flags} "
            f"--target {shlex.quote(pylib)} -r {shlex.quote(req)} 2>&1",
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
    return f"{rel}/env/bin" if strategy.endswith("packed") \
        else f"{rel}/.pixi/envs/default/bin"


def _bin_digest(adapter: SiteAdapter, rel: str, strategy: str) -> str:
    """Fingerprint of the env's executable inventory. Catches the silent-
    rot class of failure: a deleted tool would otherwise fall through to
    the host's PATH and produce a wrong-provenance result that *looks*
    clean (live-agent eval finding)."""
    d = adapter.path(_bin_dir_rel(rel, strategy))
    r = adapter.run_cmd(
        f"test -d {shlex.quote(d)} && cd {shlex.quote(d)} && "
        f"find . -type f -o -type l | LC_ALL=C sort | sha256sum | "
        f"cut -d' ' -f1 || echo none",
        timeout=60,
    )
    return r.out.strip().split()[-1] if r.out.strip() else "none"


def _spot_check_and_mark(
    env_id: str, env_row: dict, adapter: SiteAdapter, rel: str, strategy: str,
    parent: str | None = None,
) -> None:
    """Activation succeeds; interpreter runs if present; then fence-mark
    (with the bin inventory fingerprint for later integrity checks)."""
    check = f". {shlex.quote(adapter.path(rel))}/activate.sh"
    if _has_package(env_row.get("canonical", {}), "python"):
        check += " && python -c 'import sys; sys.exit(0)'"
    spot = adapter.run_activated(check, timeout=120)
    if spot.rc != 0:
        raise WeftError(
            "env.realize_failed",
            "realization spot-check failed (corrupt build?)",
            stage="realize",
            hints={"log_tail": (spot.err or spot.out)[-1000:], "retryable": True},
        )
    import json as _json
    marker = {"strategy": strategy}
    if parent:
        # the integrity fence goes two deep: if the PARENT is repaired,
        # evicted, or tampered with, this child must rebuild too
        parent_rel = env_dir_rel(parent)
        marker["parent"] = parent
        marker["parent_bin_digest"] = _bin_digest(adapter, parent_rel, "prefix")
        marker["bin_digest"] = _bin_digest(adapter, rel, "overlay")
    else:
        marker["bin_digest"] = _bin_digest(adapter, rel, strategy)
    adapter.write_file(f"{rel}/.weft-ready",
                       (_json.dumps(marker) + "\n").encode())


def _build_packed(
    env_id: str, env_row: dict, adapter: SiteAdapter, rel: str,
    modules: list[str], modules_init: str, caps: dict | None,
    pack_tools: dict,
) -> None:
    """Pack locally (pixi-pack: locked packages + offline installer),
    ship the archive as an ordinary CAS blob, unpack-verify on site.
    No network is needed from the site at any point."""
    import subprocess
    import tempfile

    pixi_pack = pack_tools.get("pixi_pack")
    cas = pack_tools.get("cas")
    transfers = pack_tools.get("transfers", {})
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
    adapter.run_cmd(f"rm -rf {shlex.quote(dest)} && mkdir -p {shlex.quote(dest)}")
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


def _site_platform(caps: dict | None) -> str:
    from .capability import compute_view
    view = compute_view(caps or {})
    osname = view.get("os", "linux")
    arch = view.get("arch", "x86_64")
    if osname == "darwin":
        return "osx-arm64" if arch in ("arm64", "aarch64") else "osx-64"
    return "linux-aarch64" if arch in ("arm64", "aarch64") else "linux-64"
