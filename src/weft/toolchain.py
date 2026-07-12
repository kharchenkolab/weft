"""Build-time toolchain for layer installs (design: layering §source-builds).

A source-built delta (a GitHub R package, a sdist wheel) needs a compiler.
The instinctive fix — add `c-compiler` to the child's deps — would change
the **conda layer**, which disqualifies the overlay: you would de-optimize
exactly the case you meant to optimize.

So weft brings its own compiler: a site-side conda env, built once from the
same channel the parent's packages came from (conda-forge — so the ABI
matches), put on PATH *during the layer install only*, and never present in
the child's activation. Compiles see the parent's headers/libs via
CPPFLAGS/LDFLAGS, and the resulting .so links against the parent's
libraries, which are on the runtime path because the child's activation
sources the parent's.

The build products are cached content-addressed by
`hash(source identity, parent EnvID, platform, toolchain)`, so the second
workspace — or the next colleague on a shared site — pays nothing.
"""

from __future__ import annotations

import shlex

from .adapters.base import SiteAdapter
from .errors import WeftError
from .ids import canonical_json, sha256_bytes

TOOLCHAIN_REL = "toolchain"
TOOLCHAIN_SPEC = [
    "c-compiler", "cxx-compiler", "fortran-compiler", "make", "pkg-config",
]


def toolchain_id(platform: str = "linux-64") -> str:
    """Identity of the toolchain itself — part of every compile-cache key."""
    return sha256_bytes(canonical_json(
        {"packages": sorted(TOOLCHAIN_SPEC), "platform": platform}))[:16]


def ensure_toolchain(adapter: SiteAdapter, pixi_bin: str,
                     platform: str = "linux-64") -> str | None:
    """Materialize the build toolchain on the site (once). Returns its
    prefix, or None if it cannot be built (the caller then falls back to a
    full prefix realization rather than guessing)."""
    from .realize import _SiteLease, _build_lock
    rel = f"{TOOLCHAIN_REL}/{toolchain_id(platform)}"
    if adapter.file_exists(f"{rel}/.weft-ready"):
        return adapter.path(rel)
    # one build per site — across threads (in-process lock) and across
    # users on a shared root (site-side lease): concurrent pixi installs
    # into one dir corrupt it
    with _build_lock(f"toolchain/{toolchain_id(platform)}", adapter.name):
        if adapter.file_exists(f"{rel}/.weft-ready"):
            return adapter.path(rel)
        lease = _SiteLease(adapter, rel)
        if lease.acquire_or_adopt():
            return adapter.path(rel)      # another user built it meanwhile
        try:
            manifest = (
                '[workspace]\nname = "weft-toolchain"\n'
                'channels = ["conda-forge"]\n'
                f'platforms = ["{platform}"]\n\n[dependencies]\n'
                + "".join(f'"{p}" = "*"\n' for p in TOOLCHAIN_SPEC)
            )
            adapter.write_file(f"{rel}/pixi.toml", manifest.encode())
            r = adapter.run_cmd(
                f"{shlex.quote(adapter.pixi_bin)} install --manifest-path "
                f"{shlex.quote(adapter.path(rel))}/pixi.toml 2>&1",
                timeout=3600)
            if r.rc != 0:
                return None
            adapter.write_file(f"{rel}/.weft-ready", b'{"kind": "toolchain"}\n')
            return adapter.path(rel)
        finally:
            lease.release()


def toolchain_fingerprint(adapter: SiteAdapter,
                          platform: str = "linux-64") -> str | None:
    """Identity of the toolchain AS RESOLVED on this site (its lockfile),
    not as requested: two sites solving 'c-compiler = *' a year apart get
    different gcc — their build artifacts must not share a cache key."""
    rel = f"{TOOLCHAIN_REL}/{toolchain_id(platform)}"
    r = adapter.run_cmd(
        f"sha256sum {shlex.quote(adapter.path(rel))}/pixi.lock 2>/dev/null "
        f"| cut -d' ' -f1", timeout=60)
    return r.out.strip() or None


def build_env_prelude(adapter: SiteAdapter, toolchain_prefix: str,
                      parent_prefix: str) -> str:
    """Shell prelude that puts the compiler on PATH and points it at the
    PARENT's headers and libraries — the ABI contract for a layered build."""
    tc = f"{toolchain_prefix}/.pixi/envs/default"
    parent_env = f"{parent_prefix}/.pixi/envs/default"
    return (
        f'export PATH="{tc}/bin:$PATH"\n'
        f'export CPPFLAGS="-I{parent_env}/include ${{CPPFLAGS:-}}"\n'
        f'export LDFLAGS="-L{parent_env}/lib -Wl,-rpath,{parent_env}/lib '
        f'${{LDFLAGS:-}}"\n'
        f'export PKG_CONFIG_PATH="{parent_env}/lib/pkgconfig:'
        f'${{PKG_CONFIG_PATH:-}}"\n'
    )


def compile_cache_key(source_identity: dict, parent_env_id: str,
                      platform: str) -> str:
    """hash(source, parent env, platform, toolchain) — the sharing unit."""
    return sha256_bytes(canonical_json({
        "source": source_identity,
        "parent": parent_env_id,
        "platform": platform,
        "toolchain": toolchain_id(platform),
    }))


def cached_build(store, key: str) -> str | None:
    for row in store.datarefs_with_meta("compile_cache", key):
        return row["ref"]
    return None


def put_cached_build(store, cas, key: str, tar_path) -> str:
    info = cas.register_file(tar_path)
    meta = {"origin": f"compile-cache:{key}", "compile_cache": key}
    if info.plain_sha256:
        meta["sha256_plain"] = info.plain_sha256
    store.put_dataref(info.ref, "file", info.bytes, info.chunks, meta=meta)
    return info.ref
