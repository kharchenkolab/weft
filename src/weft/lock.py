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


def render_pixi_manifest(spec: EnvSpec) -> str:
    lines = [
        "[workspace]",
        f"name = {_toml_str('weft-env')}",
        f"channels = [{', '.join(_toml_str(c) for c in spec.channels)}]",
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
_NETWORK_MARKERS = ("connection", "timed out", "dns", "network", "fetch repodata")


def solve(spec: EnvSpec, workdir: Path, pixi_bin: str = "pixi") -> LockResult:
    """Solve a (fully merged) spec into a lockfile. Requires index access."""
    workdir.mkdir(parents=True, exist_ok=True)
    manifest = render_pixi_manifest(spec)
    (workdir / "pixi.toml").write_text(manifest)
    lockfile = workdir / "pixi.lock"
    if lockfile.exists():
        lockfile.unlink()
    proc = subprocess.run(
        [pixi_bin, "lock", "--manifest-path", str(workdir / "pixi.toml")],
        capture_output=True,
        text=True,
        timeout=SOLVE_TIMEOUT_S,
    )
    if proc.returncode != 0 or not lockfile.exists():
        err = (proc.stderr or proc.stdout).strip()
        tail = "\n".join(err.splitlines()[-30:])
        low = err.lower()
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
                "suggestion": "relax or remove one of the conflicting pins listed in solver_message, then re-solve",
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
