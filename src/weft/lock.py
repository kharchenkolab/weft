"""Resolution: EnvSpec -> pixi solve -> canonical lock -> EnvID.

pixi (rattler + uv) is driven as a subprocess against a rendered manifest.
The native pixi.lock is kept verbatim (realizations install from it with
`pixi install --frozen`); identity comes from our own canonical form so
lockfile format churn cannot orphan caches (doc 06 §4).

EnvID = "env:v1:" + sha256(canonical lock document). The canonical document
contains, per platform, the sorted list of (kind, name, version, build,
sha256) records, plus an `extras` block for the spec fields that escape
package locking (modules, post_install, container_base, env_vars) — those
alter what a realization does, so they must alter identity too.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import WeftError
from .ids import env_id
from .spec import EnvSpec, split_constraint

SOLVE_TIMEOUT_S = 900


def _toml_str(s: str) -> str:
    return json.dumps(s)  # valid TOML basic string


def _normalize_constraint(c: str) -> str:
    # conda's fuzzy "=3.12" / "=2.*" -> "3.12.*" / "2.*" for pixi.
    m = re.fullmatch(r"=\s*([\w.]+?)(\.\*|\*)?", c)
    if m:
        v = m.group(1)
        return v if v.endswith("*") else v + ".*"
    return c


def _dep_line(dep: str) -> str:
    """Render one conda dep. Supports 'name', 'name constraint', and
    'name constraint build-selector' ('pytorch 2.* *cuda*')."""
    name, constraint = split_constraint(dep)
    parts = constraint.split()
    if len(parts) == 2:
        version, build = parts
        return (f"{_toml_str(name)} = {{ version = "
                f"{_toml_str(_normalize_constraint(version))}, "
                f"build = {_toml_str(build)} }}")
    return f"{_toml_str(name)} = {_toml_str(_normalize_constraint(constraint))}"


def parent_pins(parent_canonical: dict, platform: str = "linux-64") -> list[str]:
    """Every package of a resolved parent, as an exact (version, build) pin.

    Feeding these back to the solver makes the child's resolution a SUPERSET
    of the parent's by construction — no base drift, so an overlay
    realization is coherent — and collapses the search space, so "add one
    package" solves in a moment instead of re-deriving the whole world.
    """
    pins = []
    for p in parent_canonical.get("platforms", {}).get(platform, []):
        if p["kind"] != "conda":
            continue                    # pypi pins go in the pypi section
        pins.append(f'{p["name"]} =={p["version"]} {p["build"]}')
    return pins


def parent_pypi_pins(parent_canonical: dict, platform: str = "linux-64") -> list[str]:
    return [f'{p["name"]} =={p["version"]}'
            for p in parent_canonical.get("platforms", {}).get(platform, [])
            if p["kind"] == "pypi"]


def synth_parent_channel(native_lock_text: str,
                         channel_dir: Path) -> tuple[str, dict[str, str]]:
    """A local conda channel synthesized from a parent's OWN lock, so an
    extends solve never depends on the parent's builds still existing in
    live repodata (bioconda rotates builds away; exact `==version build`
    pins then find no candidates and every published pack has a shelf
    life). The lock records everything repodata needs — name, version,
    build, depends, sha256 — per package.

    Returns (file:// channel URL, {synth_url: original_url}) — the map
    drives rewrite_lock_urls: packages the solver takes from this
    channel must leave the lock pointing at their REAL homes (a remote
    realize cannot fetch file://); content identity is untouched (same
    filename, same sha).
    """
    doc = yaml.safe_load(native_lock_text)
    by_subdir: dict[str, dict] = {}
    url_map: dict[str, str] = {}
    channel_dir = Path(channel_dir)
    for rec in doc.get("packages", []):
        url = rec.get("conda")
        if not url:
            continue                     # pypi rotation is a different story
        subdir, _, fname = url.rsplit("/", 2)[-2], None, url.rsplit("/", 1)[-1]
        name, version, build = _conda_url_fields(url)
        entry = {
            "name": name, "version": version, "build": build,
            "build_number": _build_number(build),
            "subdir": subdir,
            "depends": list(rec.get("depends") or []),
            **({"constrains": list(rec["constrains"])}
               if rec.get("constrains") else {}),
            **({"sha256": rec["sha256"]} if rec.get("sha256") else {}),
            **({"md5": rec["md5"]} if rec.get("md5") else {}),
            **({"license": rec["license"]} if rec.get("license") else {}),
            **({"size": rec["size"]} if rec.get("size") else {}),
        }
        key = "packages.conda" if fname.endswith(".conda") else "packages"
        by_subdir.setdefault(subdir, {"packages": {},
                                      "packages.conda": {}})[key][fname] = entry
        # pixi records file-channel packages as BARE PATHS (no scheme)
        # while channel URLs keep file:// — map both forms (probed
        # against real pixi.lock output; the uri-only first version
        # rewrote nothing and the guard caught it)
        url_map[f"{channel_dir.as_uri()}/{subdir}/{fname}"] = url
        url_map[str(channel_dir / subdir / fname)] = url
    for subdir in set(by_subdir) | {"noarch"}:   # rattler wants noarch
        d = channel_dir / subdir
        d.mkdir(parents=True, exist_ok=True)
        data = by_subdir.get(subdir, {"packages": {}, "packages.conda": {}})
        (d / "repodata.json").write_text(json.dumps({
            "info": {"subdir": subdir}, **data}, sort_keys=True))
    return channel_dir.as_uri(), url_map


def _build_number(build: str) -> int:
    tail = build.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def rewrite_lock_urls(lock_text: str, url_map: dict[str, str],
                      synth_root: str | Path | None = None) -> str:
    """Point synth-channel lock entries back at their real homes. A pure
    pointer fix — filename and sha are unchanged, so the canonical form
    (identity) is identical before and after; conformance tests assert
    the child's parent entries are field-identical to the parent lock's.
    Plain string replace is safe: the path appears verbatim in both the
    packages section and the environments section (longest keys first so
    the file:// form wins over its bare-path substring). The lock's own
    CHANNELS list keeps a line naming the synth channel — dropped: a
    remote --frozen install must never be pointed at controller disk."""
    for synth in sorted(url_map, key=len, reverse=True):
        lock_text = lock_text.replace(synth, url_map[synth])
    if synth_root is not None:
        root = str(synth_root)
        lock_text = "\n".join(
            ln for ln in lock_text.splitlines() if root not in ln) + "\n"
    return lock_text


def render_pixi_manifest(spec: EnvSpec,
                         extra_channels_first: list[str] | None = None) -> str:
    # extra_channels_first is a SOLVE-TIME mechanic (the synthesized
    # parent channel), never part of the spec: it must not perturb
    # spec_hash, and the lock URL rewrite erases it from the result —
    # identity comes from content, and the content is the parent's
    channels = list(extra_channels_first or []) + list(spec.channels)
    lines = [
        "[workspace]",
        f"name = {_toml_str('weft-env')}",
        f"channels = [{', '.join(_toml_str(c) for c in channels)}]",
        f"platforms = [{', '.join(_toml_str(p) for p in spec.platforms)}]",
    ]
    # only pixi-known keys go into the manifest; others (e.g. cran_snapshot)
    # are consumed by weft-level solvers
    _PIXI_SYSREQ = {"cuda", "libc", "linux", "macos", "archspec"}
    pixi_reqs = {k: v for k, v in spec.system_requirements.items()
                 if k in _PIXI_SYSREQ}
    if pixi_reqs:
        lines.append("")
        lines.append("[system-requirements]")
        for k, v in sorted(pixi_reqs.items()):
            lines.append(f"{k} = {_toml_str(v)}")
    lines += ["", "[dependencies]"]
    for dep in spec.conda:
        lines.append(_dep_line(dep))
    if spec.pypi:
        lines.append("")
        lines.append("[pypi-dependencies]")
        for dep in spec.pypi:
            name, constraint = split_constraint(dep)
            c = _normalize_constraint(constraint)
            lines.append(f"{_toml_str(name)} = {_toml_str('*' if c == '*' else c)}")
    for plat, v in sorted(spec.variants.items()):
        if v.get("conda"):
            lines.append("")
            lines.append(f"[target.{plat}.dependencies]")
            for dep in v["conda"]:
                lines.append(_dep_line(dep))
        if v.get("pypi"):
            lines.append("")
            lines.append(f"[target.{plat}.pypi-dependencies]")
            for dep in v["pypi"]:
                name, constraint = split_constraint(dep)
                lines.append(f"{_toml_str(name)} = {_toml_str(_normalize_constraint(constraint))}")
    return "\n".join(lines) + "\n"


_CONDA_FN_RE = re.compile(r"/([^/]+?)-([^-/]+)-([^-/]+)\.(conda|tar\.bz2)$")


def _conda_url_fields(url: str) -> tuple[str, str, str]:
    m = _CONDA_FN_RE.search(url)
    if not m:
        raise WeftError("env.solve_failed", f"unparseable conda url in lock: {url}", stage="solve")
    return m.group(1), m.group(2), m.group(3)


def canonicalize_lock(pixi_lock_text: str, spec: EnvSpec) -> dict:
    """Reduce a native pixi.lock to the canonical identity document."""
    doc = yaml.safe_load(pixi_lock_text)
    by_url: dict[str, dict] = {}
    for rec in doc.get("packages", []):
        url = rec.get("conda") or rec.get("pypi")
        if url:
            by_url[url] = rec
    # lock format v7 keys environment packages by named platform *profile*
    # (subdir + virtual packages); v6 keys by subdir directly. Our canonical
    # form always uses subdirs — this indirection is exactly the format
    # churn the canonical layer exists to absorb (doc 06 §4).
    profile_subdir = {
        p["name"]: p.get("subdir", p["name"])
        for p in (doc.get("platforms") or [])
        if isinstance(p, dict) and "name" in p
    }
    env = doc["environments"]["default"]
    platforms: dict[str, list[dict]] = {}
    for plat_key, entries in env["packages"].items():
        plat = profile_subdir.get(plat_key, plat_key)
        rows = platforms.get(plat, [])
        for entry in entries:
            if "conda" in entry:
                url = entry["conda"]
                rec = by_url.get(url, {})
                name, version, build = _conda_url_fields(url)
                rows.append(
                    {
                        "kind": "conda",
                        "name": name,
                        "version": version,
                        "build": build,
                        "sha256": rec.get("sha256") or rec.get("md5", ""),
                    }
                )
            elif "pypi" in entry:
                url = entry["pypi"]
                rec = by_url.get(url, {})
                rows.append(
                    {
                        "kind": "pypi",
                        "name": rec.get("name", url.rsplit("/", 1)[-1]),
                        "version": str(rec.get("version", "")),
                        "build": "",
                        "sha256": rec.get("sha256", ""),
                    }
                )
        rows.sort(key=lambda r: (r["kind"], r["name"], r["version"], r["build"]))
        platforms[plat] = rows
    for plat in platforms:  # dedup if several profiles share a subdir
        seen: set[tuple] = set()
        platforms[plat] = [
            r for r in platforms[plat]
            if (key := (r["kind"], r["name"], r["version"], r["build"])) not in seen
            and not seen.add(key)
        ]
    return {
        "version": 1,
        "platforms": platforms,
        "extras": {
            "modules": spec.modules,
            "post_install": spec.post_install,
            "post_install_inputs": spec.post_install_inputs,
            "container_base": spec.container_base,
            "env_vars": spec.env_vars,
        },
    }


@dataclass
class LockResult:
    env_id: str
    canonical: dict
    native_lock: str      # verbatim pixi.lock text
    manifest: str         # rendered pixi.toml used for the solve
    platforms: list[str]


_CONFLICT_MARKERS = (
    "Cannot solve the request",
    "cannot be solved",
    "no candidates were found",
    "conflict",
    "unsatisfiable",
    "failed to resolve",
)
# one owner for the network-marker vocabulary (remedies gates notes on
# it too — a second copy here drifted once already)
from .remedies import NETWORK_MARKERS as _NETWORK_MARKERS  # noqa: E402
from .remedies import solve_conflict as _remedy_solve_conflict  # noqa: E402,E501

# pixi failed READING the manifest — nothing was solved. Probed verbatim
# against real pixi: dup-key says "duplicate key"; every parse error
# carries a miette span citing the manifest with line:col (a shape solver
# conflicts never produce). Field note #5: this came back wearing
# env.solve_conflict + a soft-pin suggestion that cannot work.
_PARSE_RE = re.compile(r"pixi\.toml:\d+:\d+")

# deterministic local-cache breakage (netfs file locking), NOT a network
# fault — checked BEFORE the network heuristic. Captured verbatim from
# cbe.next (netfs-only: NFS home, BeeGFS /tmp): "failed to fetch
# conda-pypi mapping … Cache error: File still doesn't exist"
_CACHE_MARKERS = ("cache error", "file still doesn't exist",
                  "conda-pypi mapping")


def solve(spec: EnvSpec, workdir: Path, pixi_bin: str = "pixi",
          extra_channels: list[str] | None = None) -> LockResult:
    """Solve a (fully merged) spec into a lockfile. Requires index access.

    The subprocess gets a hermetic PIXI_CACHE_DIR when the default cache
    sits on a network filesystem (rattler's cache locking breaks there —
    controller-on-login-node deployments); ambient PIXI_CACHE_DIR is
    always respected as the user's explicit choice.

    extra_channels are prepended for THIS solve only (the synthesized
    parent channel of an extends solve) — never part of the spec or its
    hash; the caller rewrites the resulting lock's URLs back to real
    homes."""
    from .cachedir import local_cache_dir
    import os as _os
    workdir.mkdir(parents=True, exist_ok=True)
    # kwarg only when set: test doubles monkeypatch the renderer/solver
    # with exact-arity fakes, and the common path should stay
    # signature-identical (the round-B lane caught three such doubles)
    manifest = (render_pixi_manifest(spec,
                                     extra_channels_first=extra_channels)
                if extra_channels else render_pixi_manifest(spec))
    (workdir / "pixi.toml").write_text(manifest)
    lockfile = workdir / "pixi.lock"
    if lockfile.exists():
        lockfile.unlink()
    cache_dir, cache_why = local_cache_dir()
    env = None
    if cache_dir:
        env = {**_os.environ, "PIXI_CACHE_DIR": cache_dir}
    proc = subprocess.run(
        [pixi_bin, "lock", "--manifest-path", str(workdir / "pixi.toml")],
        capture_output=True,
        text=True,
        timeout=SOLVE_TIMEOUT_S,
        env=env,
    )
    if proc.returncode != 0 or not lockfile.exists():
        err = (proc.stderr or proc.stdout).strip()
        tail = "\n".join(err.splitlines()[-30:])
        low = err.lower()
        # forensics survive a swallowed exception (aba incident: a
        # caller ate the error; the solve dir kept pixi.toml and
        # NOTHING else — a state-DB reconstruction to explain). The
        # full stderr lives next to the manifest that produced it.
        try:
            (workdir / "solve.err").write_text(err + "\n")
        except OSError:
            pass                       # forensics must never mask the verdict
        if "is not a known platform" in err:
            # caller's platform typo — intake's shape check is
            # deliberately loose (future subdirs must not need a weft
            # release); pixi's verdict is authoritative and even lists
            # the valid set (probed verbatim)
            raise WeftError(
                "task.invalid",
                f"spec '{spec.name}' names a platform pixi does not know",
                stage="solve",
                hints={"stderr_tail": tail,
                       "platforms": list(spec.platforms)},
            )
        if "duplicate key" in low or _PARSE_RE.search(err):
            # after intake validation, a manifest pixi cannot parse is
            # weft's own renderer bug — not a statement about the
            # dependency graph, and the caller's pins are NOT implicated
            raise WeftError(
                "internal.error",
                f"weft rendered a manifest pixi could not parse for "
                f"spec '{spec.name}' — a weft bug, not a spec conflict",
                stage="solve",
                hints={"stderr_tail": tail,
                       "suggestion": "nothing was solved; do not edit "
                                     "pins — report this with the spec"},
            )
        if any(m in low for m in _CACHE_MARKERS):
            raise WeftError(
                "env.solve_failed",
                "solver cache unusable — deterministic local failure, "
                "not index reachability",
                stage="solve",
                hints={"stderr_tail": tail,
                       "cache_dir": cache_dir or "pixi default",
                       "cache_resolution": cache_why,
                       "suggestion": "point PIXI_CACHE_DIR at node-local "
                                     "storage ($XDG_RUNTIME_DIR, "
                                     "/dev/shm/weft-<uid>); network "
                                     "filesystems break rattler's cache "
                                     "locking"},
                retryable=False,
            )
        if any(m in low for m in _NETWORK_MARKERS) and not any(
            m.lower() in low for m in _CONFLICT_MARKERS
        ):
            raise WeftError(
                "env.solve_failed",
                "solver could not reach package indexes",
                stage="solve",
                hints={"stderr_tail": tail},
                retryable=True,
            )
        raise WeftError(
            "env.solve_conflict",
            f"spec '{spec.name}' is unsatisfiable as pinned",
            stage="solve",
            hints={
                "solver_message": tail,
                "user_pins": spec.conda + spec.pypi,
                # gated: "no candidates" = the name/version does not
                # EXIST — softening pins cannot conjure it (remedy
                # census; the r-signac agent got relax-advice for a
                # package conda-forge does not carry)
                # labeled separately: on the extends_env path these are
                # MACHINE-written parent pins (one map per platform) —
                # folding them into user_pins would bury the authored
                # constraints under hundreds of entries
                **({"variant_pins": {
                    p: (v.get("conda") or []) + (v.get("pypi") or [])
                    for p, v in sorted(spec.variants.items())}}
                   if spec.variants else {}),
                # weft's own one-call answer to this exact error — agents read
                # hints under pressure, not the reference docs (eval finding)
                "suggestion": _remedy_solve_conflict(
                    tail, spec.conda + spec.pypi),
            },
        )
    native = lockfile.read_text()
    canonical = canonicalize_lock(native, spec)
    return LockResult(
        env_id=env_id(canonical),
        canonical=canonical,
        native_lock=native,
        manifest=manifest,
        platforms=list(spec.platforms),
    )
