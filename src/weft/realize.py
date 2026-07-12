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

from .adapters.base import SiteAdapter
from .errors import WeftError
from .ids import ENVID_SCHEME
from .store import Store

_BUILD_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_BUILD_LOCKS_GUARD = threading.Lock()


def env_dir_rel(env_id: str) -> str:
    return f"envs/{env_id.removeprefix(ENVID_SCHEME)}"


def _build_lock(env_id: str, site: str) -> threading.Lock:
    with _BUILD_LOCKS_GUARD:
        return _BUILD_LOCKS.setdefault((env_id, site), threading.Lock())


def _has_package(canonical: dict, name: str) -> bool:
    return any(
        p["name"] == name
        for plat in canonical.get("platforms", {}).values()
        for p in plat
    )


def module_prelude(modules: list[str]) -> str:
    lines = []
    for m in modules:
        lines.append(f"module load {shlex.quote(m)} || {{ echo 'weft: module load {m} failed' >&2; exit 90; }}")
    return "\n".join(lines)


def ensure_prefix_realization(
    env_id: str, env_row: dict, adapter: SiteAdapter, store: Store,
    *, modules: list[str] | None = None,
) -> dict:
    """Idempotent: cache-hit fast path, else build under (EnvID, site) lock."""
    strategy = "modules+prefix" if modules else "prefix"
    rel = env_dir_rel(env_id)
    with _build_lock(env_id, adapter.name):
        existing = store.get_realization(env_id, adapter.name)
        if existing and existing["state"] == "ready":
            if adapter.file_exists(f"{rel}/.weft-ready"):
                return existing
            # site-side deletion (e.g. scratch purge): demote and rebuild
            store.set_realization(env_id, adapter.name, strategy, rel, "missing")
        elif adapter.file_exists(f"{rel}/.weft-ready"):
            # site has it but store forgot (crash recovery): re-adopt
            store.set_realization(env_id, adapter.name, strategy, rel, "ready")
            return store.get_realization(env_id, adapter.name)

        store.set_realization(env_id, adapter.name, strategy, rel, "building")
        try:
            _build_prefix(env_id, env_row, adapter, rel, modules or [])
        except WeftError as e:
            store.set_realization(
                env_id, adapter.name, strategy, rel, "failed", log=e.detail
            )
            raise
        store.set_realization(env_id, adapter.name, strategy, rel, "ready")
        return store.get_realization(env_id, adapter.name)


def _build_prefix(
    env_id: str, env_row: dict, adapter: SiteAdapter, rel: str, modules: list[str]
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
        activate += module_prelude(modules) + "\n"
    activate += hook.out
    adapter.write_file(f"{rel}/activate.sh", activate.encode())

    # post-build spot-check: activation succeeds; interpreter runs if present
    check = f". {shlex.quote(adapter.path(rel))}/activate.sh"
    if _has_package(env_row.get("canonical", {}), "python"):
        check += " && python -c 'import sys; sys.exit(0)'"
    spot = adapter.run_cmd(f"sh -c {shlex.quote(check)}", timeout=120)
    if spot.rc != 0:
        raise WeftError(
            "env.realize_failed",
            "realization spot-check failed (corrupt build?)",
            stage="realize",
            hints={"log_tail": (spot.err or spot.out)[-1000:], "retryable": True},
        )
    adapter.write_file(f"{rel}/.weft-ready", b'{"strategy": "prefix"}\n')
