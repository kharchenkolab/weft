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

# conda package names may start with an underscore (_openmp_mutex,
# _libgcc_mutex) — which only bites when a full lock is fed back as pins
_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_][A-Za-z0-9._-]*)\s*(.*)$")

def current_platform() -> str:
    """Conda subdir of the machine this code runs on (the controller)."""
    import platform as _platform
    arm = _platform.machine().lower() in ("arm64", "aarch64")
    if _platform.system() == "Darwin":
        return "osx-arm64" if arm else "osx-64"
    return "linux-aarch64" if arm else "linux-64"


# A spec that names no platforms follows the controller it is written on;
# targeting a foreign-platform site is an explicit declaration (platform
# membership is part of the EnvID — same spec + more platforms = new env).
DEFAULT_PLATFORMS = [current_platform()]
DEFAULT_CHANNELS = ["conda-forge"]


def split_constraint(dep: str) -> tuple[str, str]:
    """'root >=6.32' -> ('root', '>=6.32'); bare name -> (name, '*').
    A trailing '?' marks the constraint SOFT (a preference the solver may
    relax under relax="soft"); it is stripped here."""
    m = _NAME_RE.match(strip_soft(dep))
    if not m:
        raise WeftError(
            "task.invalid", f"cannot parse dependency string: {dep!r}", stage="solve"
        )
    name, rest = m.group(1), m.group(2).strip()
    return name.lower(), (rest or "*")


def is_soft(dep: str) -> bool:
    """'scipy ==1.14.1?' — a preference, not a pin. Hard pins are NEVER
    relaxed: a silent version drop is exactly what a substrate must not do."""
    return dep.rstrip().endswith("?")


def strip_soft(dep: str) -> str:
    return dep.rstrip()[:-1].rstrip() if is_soft(dep) else dep


def relax_dep(dep: str) -> str:
    """Drop a soft constraint down to a bare name (keep the package)."""
    return split_constraint(dep)[0]


def parse_cran_dep(dep: str) -> dict:
    """THE grammar for R dep strings — every lane parses with this one
    function, forwarding lanes included (a vocabulary with two parsers
    is a bug in waiting: subdir refs installed fine in sessions via
    remotes' grammar and 404'd in solves; same-owner refs collapsed in
    extends merges — 2026-07 vocabulary sweep).

    Shapes: 'name', 'name ==X.Y.Z', 'owner/repo@ref',
    'owner/repo/sub/dir@ref' (nested subdir, remotes' grammar; ref
    optional -> HEAD; a branch name may contain '/' AFTER the '@')."""
    dep = dep.strip()
    if "/" in dep.partition("@")[0]:
        path, _, ref = dep.partition("@")
        segs = path.split("/")
        if len(segs) < 2 or not all(segs) or " " in path:
            raise WeftError(
                "task.invalid", f"cannot parse github ref {dep!r}",
                stage="solve",
                hints={"expected": "owner/repo[/subdir...][@ref]"})
        return {"kind": "github", "repo": "/".join(segs[:2]),
                "subdir": "/".join(segs[2:]) or None,
                "ref": ref or "HEAD"}
    parts = dep.split()
    if len(parts) == 1:
        return {"kind": "cran", "name": parts[0], "version": None}
    name, constraint = parts[0], " ".join(parts[1:])
    if constraint.startswith("=="):
        return {"kind": "cran", "name": name,
                "version": constraint[2:].strip()}
    raise WeftError(
        "task.invalid", f"cran constraint {dep!r} not supported",
        stage="solve",
        hints={"supported": ["name", "name ==X.Y.Z",
                             "owner/repo[/subdir]@ref"],
               "note": "the dated snapshot already freezes versions — "
                       "ranges have no meaning against it"})


_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# conda platform subdirs: linux-64, osx-arm64, win-64, linux-aarch64,
# noarch... — anything else (dots, brackets, quotes) would land RAW in a
# pixi.toml [target.<plat>.dependencies] header: a `]` breaks the parse,
# a `.` silently nests a valid-but-WRONG table (2026-07 injection sweep)
_PLATFORM_RE = re.compile(r"[a-z0-9]+(-[a-z0-9_]+)*")


def request_namespace(lanes: list[str]) -> str:
    """The namespace bare names are written in, derived from the
    RANKING itself: cran in lanes => R registry names (the conda lane
    derives its dialect); lanes within {conda, pypi} => passthrough
    (those ecosystems mostly agree). cran+pypi together is AMBIGUOUS
    for a bare name — refusal, never a guess."""
    ls = set(lanes)
    if "cran" in ls and "pypi" in ls:
        raise WeftError(
            "task.invalid",
            "bare names are ambiguous across cran+pypi lanes — say which "
            "ecosystem's name you mean",
            stage="realize",
            hints={"escape": 'per-lane spellings: {"name": "X", '
                             '"pypi": "x", "cran": "X"}'})
    return "r" if "cran" in ls else "py"


def ranked_namespace(norm: list, lanes: list[str]) -> str:
    """Entry-scoped namespace resolution (a github ref is lane-tagged
    by grammar; a fully-spelled entry needs no interpretation — only a
    BARE name forces the question). ONE function for chain and probe."""
    bare = [d for d, ov in norm
            if "/" not in d.partition("@")[0]
            and not all(ov.get(ln) for ln in lanes)]
    if bare:
        return request_namespace(lanes)
    return "r" if "cran" in lanes else "py"


def lane_spelling(name: str, lane: str, namespace: str) -> str:
    """THE dialect function — one derivation used by the chain AND
    probe (a second implementation would be a split-brain on day one).
    Deterministic, documented: an R-namespace bare name on the conda
    lane follows conda's r-<lowercase> convention; everything else
    passes through. Only safe under an effective postcondition
    (verify-in-loop bounds a dialect miss) — callers enforce that."""
    if namespace == "r" and lane == "conda" and "/" not in name:
        parts = name.strip().split(None, 1)
        base, tail = parts[0], (f" {parts[1]}" if len(parts) > 1 else "")
        if not base.lower().startswith("r-"):
            base = f"r-{base.lower()}"
        return base + tail
    return name


def _dep_key(eco: str, dep: str) -> str:
    """Collision key for one dep within a lane. conda names are
    case-insensitive; pypi normalizes per PEP 503; cran/julia/... are
    case-SENSITIVE ecosystems (Matrix != matrix), so their key is the
    exact name — and a github ref keys by its SOURCE (repo+subdir):
    the same package at two refs is still the same package twice."""
    if eco == "conda":
        return split_constraint(dep)[0]
    if eco == "pypi":
        return re.sub(r"[-_.]+", "-", split_constraint(dep)[0])
    d = strip_soft(dep).strip()
    if "/" in d.partition("@")[0]:
        try:
            p = parse_cran_dep(d)
            return p["repo"] + (f"/{p['subdir']}" if p["subdir"] else "")
        except WeftError:
            return d
    return d.split()[0]


def validate_repo_urls(urls, where: str = "install"):
    """Intake guard for extra repository URLs (cran_repos): a list of
    http(s) URLs with no whitespace/control characters. Downstream the
    strings are json.dumps-quoted into R code (injection-safe), but a
    malformed URL would still be accepted-and-mangled by R's repos
    machinery — refuse at intake instead (malformed-input lane)."""
    if urls is None:
        return None
    from .errors import WeftError
    if not isinstance(urls, list) or \
            not all(isinstance(u, str) for u in urls):
        raise WeftError("task.invalid",
                        f"{where}: cran_repos must be a list of URL "
                        f"strings", stage="solve",
                        hints={"got": type(urls).__name__})
    for u in urls:
        # file:// is legitimate (air-gapped CRAN mirrors) and the spec
        # lane has always accepted it — cran_repos becomes the spec's
        # r_repositories at snapshot, ONE vocabulary
        if (not u.startswith(("http://", "https://", "file://"))
                or any(c.isspace() for c in u)
                or any(ord(c) < 32 for c in u)):
            raise WeftError("task.invalid",
                            f"{where}: not a usable repository URL",
                            stage="solve",
                            hints={"entry": u[:200],
                                   "expected": "http(s):// or file:// "
                                               "with no whitespace/"
                                               "control characters"})
    return urls


def refuse_duplicate_deps(eco: str, deps: list[str],
                          where: str = "deps") -> None:
    """A name listed twice in one lane is a malformed REQUEST — refuse
    it here, before any solver runs. Unchecked, the duplicate reached
    pixi's TOML parser and came back wearing env.solve_conflict with a
    soft-pin suggestion that cannot work (2026-07 field note #5)."""
    seen: dict[str, str] = {}
    for dep in deps:
        key = _dep_key(eco, dep)
        if key in seen:
            raise WeftError(
                "task.invalid",
                f"duplicate package {key!r} in {where}.{eco}: "
                f"{seen[key]!r} and {dep!r}",
                stage="solve",
                hints={"ecosystem": eco,
                       "duplicates": [seen[key], dep],
                       "suggestion": "one entry per package — the spec "
                                     "generator spliced the same name "
                                     "twice; keep the constraint you "
                                     "mean and drop the other"})
        seen[key] = dep


def _validated_verify(v) -> dict | None:
    if v is None:
        return None
    from .verify import validate_verify
    out = validate_verify(v)
    return out or None


def merge_verify(parent: dict | None, child: dict | None) -> dict | None:
    """Postconditions COMPOSE along the identity chain like the layers
    do (base UNION child; the child's version assertion overrides the
    base's per package) — else an extended env's realization passes
    while its inherited claims are broken."""
    if not parent:
        return child
    if not child:
        return parent
    out = {"import": list(dict.fromkeys(
               (parent.get("import") or []) + (child.get("import") or []))),
           "loads": list(dict.fromkeys(
               (parent.get("loads") or []) + (child.get("loads") or []))),
           "versions": {**(parent.get("versions") or {}),
                        **(child.get("versions") or {})}}
    return {k: v for k, v in out.items() if v} or None


def _merge_deps(parent: list[str], child: list[str],
                eco: str = "conda") -> list[str]:
    """Child-wins merge, keyed per ECOSYSTEM — keying every lane with
    split_constraint (whose name regex stops at '/' and lowercases)
    silently collapsed same-owner github refs to the OWNER and unified
    case-distinct R packages during extends/snapshot merges (2026-07
    vocabulary sweep #1: a minted EnvID lost a declared package)."""
    merged = list(parent)
    index = {_dep_key(eco, d): i for i, d in enumerate(merged)}
    for dep in child:
        name = _dep_key(eco, dep)
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
    # content-addressed inputs for post_install steps: [{ref, mount_as}].
    # THIS is what makes an escape hatch portable — the sources travel with
    # the env instead of the step secretly depending on the controller's
    # filesystem. Hashed into the EnvID (they are refs; that is honest).
    post_install_inputs: list[dict] = field(default_factory=list)
    extends: str | None = None  # "spec:v1:<sha256>" of the parent spec
    # Extend a RESOLVED env: every package in the parent's lock becomes an
    # exact pin, so the child's lock is a superset BY CONSTRUCTION (no base
    # drift) — which is what makes an overlay realization safe and a
    # "just add one package" solve fast. Hashed: it changes the resolution.
    extends_env: str | None = None   # "env:vN:<sha256>"
    # pixi [system-requirements]: lets a CUDA stack solve on a GPU-less
    # controller by asserting what the *target* provides (e.g. {"cuda": "12.4"})
    system_requirements: dict[str, str] = field(default_factory=dict)
    # R layers: extra CRAN-like repositories resolved JOINTLY with the base
    # snapshot (r-universe, drat, institutional mirrors), and repositories
    # pinned by a provider's named RELEASE line (a release freezes a
    # coherent package set — semantically a snapshot, so it pins identity
    # the same way). Both hashed: they change what resolves.
    r_repositories: list[str] = field(default_factory=list)
    r_release_repos: list[dict] = field(default_factory=list)
    # IDENTITY-NEUTRAL: why an adaptive step was taken, what to watch on a
    # re-run. Excluded from the spec hash and the EnvID (same discipline
    # that keeps site/resources out of task_hash) — documentation, never a
    # pin. `notes` is free text; `step_notes` annotates post_install by index.
    notes: list[str] = field(default_factory=list)
    step_notes: dict[str, str] = field(default_factory=dict)
    # realize POSTCONDITION (ensure_available P2): proven every time the
    # env realizes (build always; adopt per site policy). IDENTITY-
    # NEUTRAL like notes — a postcondition is a claim ABOUT the
    # artifact, not part of what it is; it must never fork the EnvID
    # or enter the hashed canonical.
    verify: dict | None = None

    # -- serialization ------------------------------------------------------

    @classmethod
    def from_dict(cls, d: dict) -> "EnvSpec":
        d = dict(d.get("envspec", d))  # accept both wrapped and bare form
        deps = d.get("deps", {}) or {}
        unknown = set(d) - {
            "name", "platforms", "channels", "deps", "variants", "modules",
            "container_base", "env_vars", "post_install", "extends",
            "system_requirements", "notes", "step_notes",
            "post_install_inputs", "extends_env",
            "r_repositories", "r_release_repos", "verify",
        }
        if unknown:
            raise WeftError(
                "task.invalid",
                f"unknown envspec fields: {sorted(unknown)}",
                stage="solve",
                hints={"known_fields": [
                    "name", "platforms", "channels", "deps.<ecosystem>",
                    "variants", "modules", "container_base", "env_vars",
                    "post_install", "post_install_inputs", "extends",
                    "extends_env", "system_requirements", "r_repositories",
                    "r_release_repos", "notes", "step_notes",
                ]},
            )
        variants = {
            plat: {"conda": list(v.get("conda", [])), "pypi": list(v.get("pypi", []))}
            for plat, v in (d.get("variants") or {}).items()
        }
        conda = [str(x) for x in deps.get("conda", [])]
        pypi = [str(x) for x in deps.get("pypi", [])]
        deps_extra = {k: [str(x) for x in v] for k, v in deps.items()
                      if k not in ("conda", "pypi") and v}
        # duplicates refused HERE, before anything solves: unchecked they
        # reached pixi's TOML parser and came back as a spec "conflict"
        refuse_duplicate_deps("conda", conda)
        refuse_duplicate_deps("pypi", pypi)
        for eco, lst in deps_extra.items():
            refuse_duplicate_deps(eco, lst)
        for plat, v in variants.items():
            refuse_duplicate_deps("conda", v["conda"],
                                  where=f"variants.{plat}")
            refuse_duplicate_deps("pypi", v["pypi"],
                                  where=f"variants.{plat}")
        platforms = list(d.get("platforms") or DEFAULT_PLATFORMS)
        for p in platforms + list(variants):
            if not _PLATFORM_RE.fullmatch(p):
                raise WeftError(
                    "task.invalid",
                    f"not a platform name: {p!r}", stage="solve",
                    hints={"examples": ["linux-64", "osx-arm64",
                                        "linux-aarch64", "win-64"]})
        for k in (d.get("env_vars") or {}):
            if not _ENV_KEY_RE.fullmatch(str(k)):
                raise WeftError(
                    "task.invalid",
                    f"env_vars key {k!r} is not a valid shell identifier",
                    stage="solve",
                    hints={"rule": "[A-Za-z_][A-Za-z0-9_]*"})
        return cls(
            name=d.get("name", "unnamed"),
            platforms=platforms,
            channels=list(d.get("channels") or DEFAULT_CHANNELS),
            conda=conda,
            pypi=pypi,
            deps_extra=deps_extra,
            variants=variants,
            modules=list(d.get("modules") or []),
            container_base=d.get("container_base"),
            env_vars={k: str(v) for k, v in (d.get("env_vars") or {}).items()},
            post_install=list(d.get("post_install") or []),
            post_install_inputs=[dict(x) for x in
                                 (d.get("post_install_inputs") or [])],
            extends=d.get("extends"),
            extends_env=d.get("extends_env"),
            r_repositories=validate_repo_urls(
                [str(x) for x in (d.get("r_repositories") or [])],
                where="spec") or [],
            r_release_repos=[dict(x) for x in
                             (d.get("r_release_repos") or [])],
            system_requirements={
                k: str(v) for k, v in (d.get("system_requirements") or {}).items()
            },
            notes=[str(n) for n in (d.get("notes") or [])],
            step_notes={str(k): str(v)
                        for k, v in (d.get("step_notes") or {}).items()},
            verify=_validated_verify(d.get("verify")),
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
            "post_install_inputs": self.post_install_inputs,
            "extends": self.extends,
            "extends_env": self.extends_env,
            "system_requirements": self.system_requirements,
            "r_repositories": self.r_repositories,
            "r_release_repos": self.r_release_repos,
            "notes": self.notes,
            "step_notes": self.step_notes,
            **({"verify": self.verify} if self.verify else {}),
        }

    IDENTITY_NEUTRAL = ("notes", "step_notes", "verify")

    def spec_hash(self) -> str:
        # notes never perturb identity: an agent may annotate a spec
        # without forking its EnvID and losing every cached realization
        body = {k: v for k, v in self.to_dict().items()
                if k not in self.IDENTITY_NEUTRAL}
        return spec_id(body)

    # -- composition --------------------------------------------------------

    def merged_onto(self, parent: "EnvSpec") -> "EnvSpec":
        """Return self layered on parent (self's fields win per the algebra)."""
        variants: dict[str, dict[str, list[str]]] = {
            p: {"conda": list(v["conda"]), "pypi": list(v["pypi"])}
            for p, v in parent.variants.items()
        }
        for plat, v in self.variants.items():
            base = variants.setdefault(plat, {"conda": [], "pypi": []})
            base["conda"] = _merge_deps(base["conda"], v.get("conda", []),
                                        "conda")
            base["pypi"] = _merge_deps(base["pypi"], v.get("pypi", []),
                                       "pypi")
        return EnvSpec(
            name=self.name if self.name != "unnamed" else parent.name,
            platforms=_prepend_unique(parent.platforms, self.platforms),
            channels=_prepend_unique(self.channels, parent.channels),
            conda=_merge_deps(parent.conda, self.conda, "conda"),
            pypi=_merge_deps(parent.pypi, self.pypi, "pypi"),
            deps_extra={
                eco: _merge_deps(parent.deps_extra.get(eco, []),
                                 self.deps_extra.get(eco, []), eco)
                for eco in {**parent.deps_extra, **self.deps_extra}
            },
            variants=variants,
            modules=parent.modules + [m for m in self.modules if m not in parent.modules],
            container_base=self.container_base or parent.container_base,
            env_vars={**parent.env_vars, **self.env_vars},
            verify=merge_verify(parent.verify, self.verify),
            post_install=parent.post_install + self.post_install,
            post_install_inputs=parent.post_install_inputs
            + self.post_install_inputs,
            extends=None,  # fully merged specs stand alone
            extends_env=self.extends_env or parent.extends_env,
            system_requirements={**parent.system_requirements,
                                 **self.system_requirements},
            r_repositories=_prepend_unique(self.r_repositories,
                                           parent.r_repositories),
            r_release_repos=self.r_release_repos + [
                r for r in parent.r_release_repos
                if r not in self.r_release_repos],
            notes=parent.notes + self.notes,
            # child steps land AFTER the parent's in the merged list — their
            # notes shift with them (else they annotate, and clobber notes
            # on, the parent's steps)
            step_notes={**parent.step_notes,
                        **{(str(int(k) + len(parent.post_install))
                            if k.isdigit() else k): v
                           for k, v in self.step_notes.items()}},
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
