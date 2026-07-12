"""EnvSpec: declarative environment description and composition algebra.

Layering (design doc 03 §2) is spec composition + whole-spec re-resolution,
never in-place installs. Merge rules are simple and total:
  - child channels prepend (deduplicated, order preserved)
  - dependency lists concatenate; a child constraint on the same package
    name replaces the parent's constraint in place
  - platforms/scalars: child overrides when set
  - modules: order-preserving union
  - env_vars: dict merge, child wins
  - post_install: parent then child, concatenated
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .errors import WeftError
from .ids import spec_id

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)$")

DEFAULT_PLATFORMS = ["linux-64"]
DEFAULT_CHANNELS = ["conda-forge"]


def split_constraint(dep: str) -> tuple[str, str]:
    """'root >=6.32' -> ('root', '>=6.32'); bare name -> (name, '*')."""
    m = _NAME_RE.match(dep)
    if not m:
        raise WeftError(
            "task.invalid", f"cannot parse dependency string: {dep!r}", stage="solve"
        )
    name, rest = m.group(1), m.group(2).strip()
    return name.lower(), (rest or "*")


def _merge_deps(parent: list[str], child: list[str]) -> list[str]:
    merged = list(parent)
    index = {split_constraint(d)[0]: i for i, d in enumerate(merged)}
    for dep in child:
        name, _ = split_constraint(dep)
        if name in index:
            merged[index[name]] = dep
        else:
            index[name] = len(merged)
            merged.append(dep)
    return merged


def _prepend_unique(child: list[str], parent: list[str]) -> list[str]:
    out = list(child)
    out.extend(c for c in parent if c not in out)
    return out


@dataclass
class EnvSpec:
    name: str = "unnamed"
    platforms: list[str] = field(default_factory=lambda: list(DEFAULT_PLATFORMS))
    channels: list[str] = field(default_factory=lambda: list(DEFAULT_CHANNELS))
    conda: list[str] = field(default_factory=list)
    pypi: list[str] = field(default_factory=list)
    # further ecosystems (cran, julia, ...) resolved by registered Solvers;
    # kept separate so conda/pypi-only specs hash exactly as before
    deps_extra: dict[str, list[str]] = field(default_factory=dict)
    # platform -> {"conda": [...], "pypi": [...]}
    variants: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    modules: list[str] = field(default_factory=list)
    container_base: str | None = None
    env_vars: dict[str, str] = field(default_factory=dict)
    post_install: list[str] = field(default_factory=list)
    extends: str | None = None  # "spec:v1:<sha256>" of the parent spec
    # pixi [system-requirements]: lets a CUDA stack solve on a GPU-less
    # controller by asserting what the *target* provides (e.g. {"cuda": "12.4"})
    system_requirements: dict[str, str] = field(default_factory=dict)

    # -- serialization ------------------------------------------------------

    @classmethod
    def from_dict(cls, d: dict) -> "EnvSpec":
        d = dict(d.get("envspec", d))  # accept both wrapped and bare form
        deps = d.get("deps", {}) or {}
        unknown = set(d) - {
            "name", "platforms", "channels", "deps", "variants", "modules",
            "container_base", "env_vars", "post_install", "extends",
            "system_requirements",
        }
        if unknown:
            raise WeftError(
                "task.invalid",
                f"unknown envspec fields: {sorted(unknown)}",
                stage="solve",
                hints={"known_fields": [
                    "name", "platforms", "channels", "deps.conda", "deps.pypi",
                    "variants", "modules", "container_base", "env_vars",
                    "post_install", "extends", "system_requirements",
                ]},
            )
        variants = {
            plat: {"conda": list(v.get("conda", [])), "pypi": list(v.get("pypi", []))}
            for plat, v in (d.get("variants") or {}).items()
        }
        return cls(
            name=d.get("name", "unnamed"),
            platforms=list(d.get("platforms") or DEFAULT_PLATFORMS),
            channels=list(d.get("channels") or DEFAULT_CHANNELS),
            conda=[str(x) for x in deps.get("conda", [])],
            pypi=[str(x) for x in deps.get("pypi", [])],
            deps_extra={k: [str(x) for x in v] for k, v in deps.items()
                        if k not in ("conda", "pypi") and v},
            variants=variants,
            modules=list(d.get("modules") or []),
            container_base=d.get("container_base"),
            env_vars={k: str(v) for k, v in (d.get("env_vars") or {}).items()},
            post_install=list(d.get("post_install") or []),
            extends=d.get("extends"),
            system_requirements={
                k: str(v) for k, v in (d.get("system_requirements") or {}).items()
            },
        )

    def to_dict(self) -> dict:
        deps: dict = {"conda": self.conda, "pypi": self.pypi}
        deps.update({k: v for k, v in sorted(self.deps_extra.items()) if v})
        return {
            "name": self.name,
            "platforms": self.platforms,
            "channels": self.channels,
            "deps": deps,
            "variants": self.variants,
            "modules": self.modules,
            "container_base": self.container_base,
            "env_vars": self.env_vars,
            "post_install": self.post_install,
            "extends": self.extends,
            "system_requirements": self.system_requirements,
        }

    def spec_hash(self) -> str:
        return spec_id(self.to_dict())

    # -- composition --------------------------------------------------------

    def merged_onto(self, parent: "EnvSpec") -> "EnvSpec":
        """Return self layered on parent (self's fields win per the algebra)."""
        variants: dict[str, dict[str, list[str]]] = {
            p: {"conda": list(v["conda"]), "pypi": list(v["pypi"])}
            for p, v in parent.variants.items()
        }
        for plat, v in self.variants.items():
            base = variants.setdefault(plat, {"conda": [], "pypi": []})
            base["conda"] = _merge_deps(base["conda"], v.get("conda", []))
            base["pypi"] = _merge_deps(base["pypi"], v.get("pypi", []))
        return EnvSpec(
            name=self.name if self.name != "unnamed" else parent.name,
            platforms=_prepend_unique(parent.platforms, self.platforms),
            channels=_prepend_unique(self.channels, parent.channels),
            conda=_merge_deps(parent.conda, self.conda),
            pypi=_merge_deps(parent.pypi, self.pypi),
            deps_extra={
                eco: _merge_deps(parent.deps_extra.get(eco, []),
                                 self.deps_extra.get(eco, []))
                for eco in {**parent.deps_extra, **self.deps_extra}
            },
            variants=variants,
            modules=parent.modules + [m for m in self.modules if m not in parent.modules],
            container_base=self.container_base or parent.container_base,
            env_vars={**parent.env_vars, **self.env_vars},
            post_install=parent.post_install + self.post_install,
            extends=None,  # fully merged specs stand alone
            system_requirements={**parent.system_requirements,
                                 **self.system_requirements},
        )

    def weakly_reproducible(self) -> bool:
        """post_install escapes locking; modules are attested, not hashed."""
        return bool(self.post_install)


def resolve_extends(
    spec: EnvSpec, lookup: Callable[[str], EnvSpec | None], _depth: int = 0
) -> EnvSpec:
    """Flatten an extends-chain into one standalone spec (cycle-safe)."""
    if spec.extends is None:
        return spec
    if _depth > 32:
        raise WeftError(
            "task.invalid", "extends chain too deep (cycle?)", stage="solve"
        )
    parent = lookup(spec.extends)
    if parent is None:
        raise WeftError(
            "task.invalid",
            f"parent spec not found: {spec.extends}",
            stage="solve",
            hints={"missing": spec.extends},
        )
    parent = resolve_extends(parent, lookup, _depth + 1)
    return spec.merged_onto(parent)
