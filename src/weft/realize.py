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

    rel = env_dir_rel(env_id)
    with _build_lock(env_id, adapter.name):
        existing = store.get_realization(env_id, adapter.name)
        if existing and existing["state"] == "ready":
            if adapter.file_exists(f"{rel}/.weft-ready"):
                store.touch_realization(env_id, adapter.name)  # LRU recency
                # integrity fence: a tampered env (deleted tool, partial
                # purge) must rebuild, not silently fall through to host
                # binaries with the locked env's name on the manifest
                import json as _json
                try:
                    marker = _json.loads(
                        adapter.read_file(f"{rel}/.weft-ready").decode())
                except (ValueError, WeftError):
                    marker = {}
                recorded = marker.get("bin_digest")
                if not recorded or recorded == _bin_digest(
                        adapter, rel, marker.get("strategy", strategy)):
                    return existing
                store.emit("realize.integrity_failed", env_id=env_id,
                           site=adapter.name,
                           note="executable inventory changed; rebuilding")
            # site-side deletion (e.g. scratch purge): demote and rebuild
            store.set_realization(env_id, adapter.name, strategy, rel, "missing")
        elif adapter.file_exists(f"{rel}/.weft-ready"):
            # site has it but store forgot (crash recovery, or another
            # workspace/user built it): re-adopt
            store.set_realization(env_id, adapter.name, strategy, rel, "ready")
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
                store.set_realization(env_id, adapter.name, strategy, rel,
                                      "ready")
                store.emit("realize.adopted", env_id=env_id,
                           site=adapter.name, via="shared-lease")
                return store.get_realization(env_id, adapter.name)
        store.set_realization(env_id, adapter.name, strategy, rel, "building")
        modules_init = (site_config or {}).get("modules_init", "")
        # an archived env rebuilds from the controller's blob — no site
        # network, even for a `prefix`-strategy site (eviction with
        # archive=True is the air-gapped reclamation path)
        archive_ref = _archived_ref(store, env_id)
        if archive_ref and not strategy.endswith("packed"):
            strategy = ("modules+packed" if modules else "packed")
            store.set_realization(env_id, adapter.name, strategy, rel,
                                  "building")
        try:
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
            _stage_post_install_inputs(env_row, adapter, rel, pack_tools or {})
            _run_post_install(env_row, adapter, rel)
            _spot_check_and_mark(env_id, env_row, adapter, rel, strategy)
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


def _archived_ref(store: Store, env_id: str) -> str | None:
    """Did someone evict this env with archive=True? Then the blob is the
    fastest (and, on air-gapped sites, the only) way back."""
    from .evict import ARCHIVE_META
    for row in store.datarefs_with_meta(ARCHIVE_META, env_id):
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


def _bin_dir_rel(rel: str, strategy: str) -> str:
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
    env_id: str, env_row: dict, adapter: SiteAdapter, rel: str, strategy: str
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
    marker = _json.dumps({"strategy": strategy,
                          "bin_digest": _bin_digest(adapter, rel, strategy)})
    adapter.write_file(f"{rel}/.weft-ready", (marker + "\n").encode())


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
