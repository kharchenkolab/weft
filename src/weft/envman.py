"""Environment manager: the spec -> EnvID pipeline with caching.

A spec is re-solved only on explicit request (`update=True`); otherwise a
previously solved spec returns its EnvID in milliseconds — task submission
stays deterministic (doc 03 §3).
"""

from __future__ import annotations

from pathlib import Path

from .errors import WeftError
from .lock import solve
from .spec import EnvSpec, resolve_extends
from .store import Store


def _satisfies(version: str, constraint: str) -> bool:
    """Does the parent's pinned version satisfy the delta's constraint?
    Conda's fuzzy '=3.12' means '3.12.*'; the rest is PEP440-ish."""
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version

    from .lock import _normalize_constraint
    c = _normalize_constraint(constraint.strip())
    if not c or c == "*":
        return True
    if not c[0] in "<>=!~":
        c = "==" + c                       # bare version means exactly that
    if c.endswith(".*") and c.startswith("=="):
        pass                               # SpecifierSet handles ==X.Y.*
    try:
        return Version(version) in SpecifierSet(c)
    except (InvalidSpecifier, InvalidVersion):
        return False                       # can't prove it: treat as a move


def _conda_provided(canonical: dict) -> dict:
    """{conda_name: version} for packages the base layer resolves on
    EVERY locked platform — what eco layers delta against (aba 1.2:
    25/26 cran installs re-built conda-provided packages from source).
    A platform-partial package stays OUT of this set so the layer still
    carries it for the platform conda leaves bare; version is taken from
    the first platform (delta facts, not identity — the lock's own
    records stay the truth per platform)."""
    plats = list((canonical.get("platforms") or {}).values())
    if not plats:
        return {}
    names = set.intersection(*({p["name"] for p in plat} for plat in plats))
    first = {p["name"]: p.get("version", "") for p in plats[0]}
    return {n: first.get(n, "") for n in names}


def _pep503(name: str) -> str:
    """PEP-503 normalization: the lock stores 'typing-extensions', a spec
    may say 'typing_extensions' — same package."""
    import re
    return re.sub(r"[-_.]+", "-", name).lower()


def _layer_dep_name(dep: str) -> str:
    """The package name a layer dep string refers to: 'glue ==1.7.0' →
    glue, 'tidyverse/glue@abc123' → glue, 'owner/Repo.jl@ref' → Repo,
    'owner/mono/rpkg@v2' → rpkg (subdir refs: the LAST path segment is
    the best string-derived guess at the DESCRIPTION name)."""
    if "/" in dep.partition("@")[0]:
        tail = dep.partition("@")[0].split("/")[-1]
        return tail[:-3] if tail.endswith(".jl") else tail
    from .spec import split_constraint
    return split_constraint(dep)[0]


def diff_envs(old_canonical: dict, new_canonical: dict) -> dict:
    """Package-level delta between two resolved envs — what an agent (and a
    user) needs to judge whether the near-match is acceptable."""
    def flat(c):
        # key by (platform, kind, name): multi-platform envs can move a
        # package on one platform only, and a conda and pypi package can
        # legitimately share a name — neither may mask the other
        out = {}
        for plat, pkgs in c.get("platforms", {}).items():
            for p in pkgs:
                out[f"{plat}/{p.get('kind', 'pkg')}:{p['name']}"] = \
                    p["version"]
        for eco, layer in (c.get("layers") or {}).items():
            for r in layer.get("records", []):
                out[f"{eco}:{r['name']}"] = r["version"]
        return out

    a, b = flat(old_canonical), flat(new_canonical)
    changed = [{"name": k, "from": a[k], "to": b[k]}
               for k in sorted(a.keys() & b.keys()) if a[k] != b[k]]
    return {
        "changed": changed,
        "added": sorted(b.keys() - a.keys()),
        "removed": sorted(a.keys() - b.keys()),
    }


class EnvManager:
    def __init__(self, store: Store, solve_dir: Path, pixi_bin: str,
                 solvers: dict | None = None, cas=None):
        self.store = store
        self.solve_dir = Path(solve_dir)
        self.pixi_bin = pixi_bin
        self.cas = cas   # workspace CAS: url-pin wheels carry here
        from .solvers import default_solvers
        self.solvers: dict = {**default_solvers(pixi_bin), **(solvers or {})}

    def _lookup_spec(self, spec_hash: str) -> EnvSpec | None:
        body = self.store.get_spec(spec_hash)
        return EnvSpec.from_dict(body) if body else None

    @staticmethod
    def _channel_hint(spec: EnvSpec) -> dict | None:
        """Spec-hygiene warning (aba2's suggestion after their isolated-
        env incident): bioconductor-* packages live ONLY on bioconda —
        a hand-authored spec naming them without the channel gets a
        conflict/no-candidates later with no pointer to the real cause.
        Deliberately scoped to bioconductor-* (r-* also lives on
        conda-forge; warning there would false-fire on every pure
        conda-forge R spec). A warning, never a refusal."""
        from .spec import split_constraint
        conda_deps = list(spec.conda) + [
            d for v in (spec.variants or {}).values()
            for d in (v.get("conda") or [])]
        bio = sorted({split_constraint(d)[0] for d in conda_deps
                      if split_constraint(d)[0].startswith("bioconductor-")})
        if bio and not any("bioconda" in c for c in spec.channels):
            return {"packages": bio,
                    "note": "bioconductor-* packages are published on the "
                            "bioconda channel, which this spec does not "
                            "list — the solve will not find them",
                    "fix": "add \"bioconda\" to the spec's channels "
                           "(conda-forge first, bioconda second is the "
                           "conventional order)"}
        return None

    def _pin_to_parent(self, spec: EnvSpec, parent_env: dict) -> EnvSpec:
        """Freeze the base: parent's exact packages + the child's delta.

        A delta constraint on a package the parent already has is either
        redundant (the pinned version satisfies it — we drop it and keep the
        pin) or a request to MOVE THE BASE, which `extends_env` exists to
        prevent: that is an immediate env.layer_conflict, never a silent
        version change.
        """
        from copy import deepcopy

        from .lock import parent_pins, parent_pypi_pins
        from .spec import split_constraint
        out = deepcopy(spec)
        canonical = parent_env["canonical"]
        parent_spec = self._lookup_spec(parent_env["spec_hash"])
        # PER-PLATFORM pins (consumer audit 2026-08-25): the old
        # platforms[0] choice applied ONE platform's exact build
        # strings to EVERY declared platform's solve — a zero-add
        # snapshot of a [linux-64, osx-arm64] pack (the ordinary
        # published shape) asked osx-arm64 to satisfy linux builds
        # ("No candidates for _openmp_mutex ==4.5 20_gnu") and could
        # never resolve. Latent since the feature (every fixture was
        # single-platform, where platforms[0] is always right); the
        # snapshot's parent-platform declaration made it the default
        # path. Each platform now pins from ITS OWN lock via variants
        # ([target.<plat>.dependencies]); shared deps carry only the
        # delta.
        locked = set((canonical.get("platforms") or {}))
        plats = [p for p in (spec.platforms or ["linux-64"])
                 if p in locked] or (sorted(locked)[:1] or ["linux-64"])
        # the extends remedy must name a door that OPENS: on an
        # adopt-only workspace "re-ensure the parent's SPEC" raises
        # parent-spec-not-found (the very failure the adopt round
        # fixed by moving away from)
        from .remedies import move_base as _move_base
        move_base = _move_base(parent_spec is not None)

        def pin_maps(pins_fn):
            return {p: {split_constraint(x)[0]: split_constraint(x)[1]
                        for x in pins_fn(canonical, p)} for p in plats}

        def check_delta(delta: list[str], by_plat: dict,
                        kind: str) -> list[str]:
            """Delta vs EVERY platform's pins: conflicting anywhere =>
            layer_conflict naming the platform; pinned-and-satisfied
            EVERYWHERE => redundant, dropped (the pin stands); new on
            any platform => kept in shared deps (the per-target pin
            outranks it where the parent already provides it)."""
            keep = []
            for dep in delta:
                name, constraint = split_constraint(dep)
                satisfied_everywhere = bool(by_plat)
                for p, pinned in by_plat.items():
                    if name not in pinned:
                        satisfied_everywhere = False
                        continue
                    version = pinned[name].lstrip("=").split()[0]
                    if constraint == "*" or _satisfies(version, constraint):
                        continue
                    raise WeftError(
                        "env.layer_conflict",
                        f"the delta asks for {kind} {name} {constraint}, "
                        f"but the parent has it pinned at {version} "
                        f"on {p}",
                        stage="solve",
                        hints={"parent": spec.extends_env,
                               "package": name, "platform": p,
                               "parent_version": version,
                               "requested": constraint,
                               "suggestion": move_base})
                if not satisfied_everywhere:
                    keep.append(dep)
            return keep

        conda_by_plat = pin_maps(parent_pins)
        out.conda = check_delta(spec.conda, conda_by_plat, "conda")
        for p in plats:
            v = dict(out.variants.get(p) or {})
            child_variant = check_delta(list(v.get("conda") or []),
                                        {p: conda_by_plat[p]}, "conda")
            v["conda"] = parent_pins(canonical, p) + child_variant
            out.variants[p] = v
        # pypi pins are version-only and the manifest has no per-target
        # pypi table: pin the versions COMMON to every platform; a
        # cross-platform divergent pypi version stays UNPINNED (rare —
        # noted honestly rather than poisoning other platforms' solves
        # with one platform's version)
        pypi_by_plat = pin_maps(parent_pypi_pins)
        kept_pypi = check_delta(spec.pypi, pypi_by_plat, "pypi")
        pypi_sets = [set(parent_pypi_pins(canonical, p)) for p in plats]
        common_pypi = sorted(set.intersection(*pypi_sets)) if pypi_sets \
            else []
        out.pypi = common_pypi + kept_pypi
        # channels inherit (child's prepend, like the spec-hash extends
        # merge): a child spec authored without the parent's bioconda
        # made every parent bioconda package invisible to the DELTA
        # solve. Solve-side only: merged_hash was captured before
        # pinning, so identity is the authored spec's. When the parent
        # SPEC is absent (adopt-only workspace), the channels come from
        # the parent's own LOCK — pixi records the channels that
        # actually solved it (consumer audit: canonical carries no
        # channels, so the synth spec ran channel-less and channel_hint
        # fired on a pack whose spec lists bioconda).
        from .spec import _prepend_unique
        if parent_spec is not None:
            out.channels = _prepend_unique(out.channels,
                                           parent_spec.channels)
        elif parent_env.get("native_lock"):
            try:
                import yaml as _yaml
                doc = _yaml.safe_load(parent_env["native_lock"]) or {}
                urls = [str(c.get("url", "")).rstrip("/") for c in
                        ((doc.get("environments") or {})
                         .get("default", {}).get("channels") or [])]
                urls = [u for u in urls if u]
                if urls:
                    out.channels = _prepend_unique(out.channels, urls)
            except Exception:   # noqa: BLE001 — inheritance is
                pass            # best-effort; the solve stays honest
        # the parent's extras carry over and MERGE with the child's: the
        # child's identity must account for everything the parent's does
        # (post_install products, modules), or the same child EnvID behaves
        # differently as an overlay vs a full prefix
        extras = canonical.get("extras", {})
        out.modules = list(extras.get("modules") or []) + [
            m for m in out.modules if m not in (extras.get("modules") or [])]
        out.env_vars = {**(extras.get("env_vars") or {}), **out.env_vars}
        n_parent_steps = len(extras.get("post_install") or [])
        out.post_install = list(extras.get("post_install") or []) \
            + out.post_install
        out.step_notes = {(str(int(k) + n_parent_steps)
                           if k.isdigit() else k): v
                          for k, v in out.step_notes.items()}
        seen_inputs = {i.get("sha256") for i in out.post_install_inputs}
        out.post_install_inputs = [
            i for i in (extras.get("post_install_inputs") or [])
            if i.get("sha256") not in seen_inputs] + out.post_install_inputs

        # parent's language layers are inherited AS EXACT PINS — inheriting
        # bare names would re-solve them against a fresh snapshot/registry
        # and silently move the base (and a github build would silently
        # become the same-versioned release from the index)
        for eco, layer in (canonical.get("layers") or {}).items():
            solver = self.solvers.get(eco)
            if solver is not None and hasattr(solver, "inherit_pins"):
                pins, sysreq = solver.inherit_pins(layer)
                for k, v in sysreq.items():
                    out.system_requirements.setdefault(k, v)
            else:
                pins = list(layer.get("top_level") or [])
            # solver-declared spec fields a child must inherit to re-solve
            # against the SAME universe (extra repos, release lines): the
            # parent's setting applies unless the child overrides it
            for k, v in (layer.get("spec_config") or {}).items():
                if not getattr(out, k, None):
                    setattr(out, k, v)
            child = out.deps_extra.get(eco, [])
            merged, pinned_names = [], {}
            for p in pins:
                pinned_names[_layer_dep_name(p)] = p
                merged.append(p)
            for dep in child:
                name = _layer_dep_name(dep)
                if name not in pinned_names:
                    merged.append(dep)
                    continue
                pin = pinned_names[name]
                if dep == pin or split_constraint(dep)[1] == "*":
                    continue          # redundant: the frozen base has it
                if _satisfies(pin.split("==")[-1].strip(),
                              split_constraint(dep)[1]) \
                        and "/" not in dep:
                    continue
                raise WeftError(
                    "env.layer_conflict",
                    f"the delta asks for {eco} {dep}, but the parent has "
                    f"{pin} frozen", stage="solve",
                    # move_base, NOT an inline copy: this was the THIRD
                    # hardcoded "re-ensure with `extends`" — a shut door
                    # on adopt-only workspaces — found by the remedy
                    # sweep AFTER the #118 fix claimed both sites (there
                    # were four; prose copies drift past greps)
                    hints={"parent": spec.extends_env, "package": name,
                           "parent_pin": pin, "requested": dep,
                           "suggestion": move_base})
            out.deps_extra[eco] = merged
        return out

    def _solve_forgiving(self, merged: EnvSpec, workdir: Path, relax: str,
                         extra_channels: list[str] | None = None):
        """Solve as written; under relax="soft", greedily drop SOFT
        constraints (trailing '?') until it solves. Hard pins are never
        touched — a silent version drop is precisely what a substrate must
        not do. The result is still fully pinned: adaptiveness lives in the
        path to a solve, not in what you got."""
        from .spec import is_soft, relax_dep
        kw = {"extra_channels": extra_channels} if extra_channels else {}
        try:
            return solve(merged, workdir, self.pixi_bin, **kw), []
        except WeftError as first:
            if relax != "soft" or first.code != "env.solve_conflict":
                raise
            soft_idx = [(eco, i, d)
                        for eco, deps in (("conda", merged.conda),
                                          ("pypi", merged.pypi))
                        for i, d in enumerate(deps) if is_soft(d)]
            if not soft_idx:
                first.hints["relax"] = (
                    "no soft constraints to relax — mark preferences with a "
                    "trailing '?' (e.g. \"scipy ==1.14.1?\") to let weft "
                    "relax them")
                raise
            relaxed: list[dict] = []
            for eco, i, dep in soft_idx:
                deps = merged.conda if eco == "conda" else merged.pypi
                requested = dep
                deps[i] = relax_dep(dep)
                from .spec import strip_soft
                relaxed.append({"dep": strip_soft(requested),
                                "ecosystem": eco,
                                "relaxed_to": deps[i]})
                try:
                    result = solve(merged, workdir, self.pixi_bin, **kw)
                except WeftError as e:
                    if e.code != "env.solve_conflict":
                        raise    # network/index trouble is NOT "still
                                 # conflicting" — misdiagnosing a transient
                                 # failure as unsatisfiable sends the agent
                                 # down the wrong repair path
                    continue     # still conflicting: relax the next one too
                for r in relaxed:
                    want = _pep503(r["relaxed_to"])
                    r["got"] = next(
                        (p["version"] for plat in
                         result.canonical["platforms"].values()
                         for p in plat if _pep503(p["name"]) == want), None)
                return result, relaxed
            first.hints["tried_relaxing"] = [r["dep"] for r in relaxed]
            first.hints["suggestion"] = (
                "soft constraints relax VERSIONS only — packages are "
                "never dropped (a silent removal would answer a different "
                "spec than yours). Version-relaxing did not solve this: "
                "a hard pin, a package with no candidates at any version, "
                "or the set itself is the conflict; the solver_message "
                "names it — delete the offending dep to proceed without it")
            raise

    def ensure(self, spec_or_id, *, update: bool = False,
               dry_run: bool = False, relax: str = "none") -> dict:
        """Accepts an EnvID string or a spec dict; returns {env_id, status, summary}.
        dry_run solves everything but stores nothing — cheap fix-testing."""
        if relax not in ("none", "soft"):
            # unvalidated, relax="Soft"/"yes"/"all" silently meant NO
            # relaxation — the caller got the exact conflict they asked
            # to avoid, with nothing saying the flag was ignored
            # (vocab-sweep find B1: worst tier, silent wrong behavior)
            raise WeftError(
                "task.invalid",
                f"relax must be 'none' or 'soft', got {relax!r}",
                stage="solve",
                hints={"known": ["none", "soft"],
                       "note": "soft relaxes VERSIONS of '?'-marked "
                               "constraints only; packages are never "
                               "dropped"})
        if isinstance(spec_or_id, str):
            row = self.store.get_env(spec_or_id)
            if not row:
                raise WeftError(
                    "task.invalid", f"unknown EnvID: {spec_or_id}", stage="solve",
                    hints={"suggestion": "pass the spec to env.ensure to (re)solve it"},
                )
            return {"env_id": spec_or_id, "status": "cached",
                    "summary": self._summary(row)}

        spec = EnvSpec.from_dict(spec_or_id)
        merged = resolve_extends(spec, self._lookup_spec)
        merged_hash = merged.spec_hash()
        # capture the body NOW: _pin_to_parent rewrites deps in place below,
        # and the stored body must hash to its key (notes may differ — they
        # are identity-neutral by design, and last-write-wins is the point)
        merged_body, merged_name = merged.to_dict(), merged.name

        # unknown ecosystems fail before any solving is paid for
        unknown = set(merged.deps_extra) - set(self.solvers)
        if unknown:
            raise WeftError(
                "task.invalid",
                f"no solver registered for ecosystem(s): {sorted(unknown)}",
                stage="solve",
                hints={"registered": sorted(self.solvers),
                       "suggestion": "typo in a deps key, or the solver "
                                     "needs to be enabled/installed"},
            )
        if not update and not dry_run:
            cached = self.store.env_for_spec(merged_hash)
            if cached:
                # persist identity-neutral annotations even on a cache hit —
                # "annotate without forking the EnvID" must actually store
                self.store.put_spec(spec.spec_hash(), spec.name,
                                    spec.to_dict())
                self.store.put_spec(merged_hash, merged_name, merged_body)
                hint = self._channel_hint(merged)
                return {"env_id": cached, "status": "cached",
                        "summary": self._summary(self.store.get_env(cached)),
                        **({"channel_hint": hint} if hint else {})}

        # extends_env: pin the parent's resolution, solve only the delta
        parent_env = None
        synth_url_map: dict[str, str] = {}
        extra_channels: list[str] | None = None
        if merged.extends_env:
            parent_env = self.store.get_env(merged.extends_env)
            if not parent_env:
                raise WeftError(
                    "task.invalid",
                    f"unknown parent EnvID: {merged.extends_env}",
                    stage="solve",
                    hints={"suggestion": "extends_env takes a resolved EnvID; "
                                         "use `extends` for a spec hash"})
            merged = self._pin_to_parent(merged, parent_env)

        # after pinning, so an inherited interpreter (r-base via extends_env)
        # satisfies a layer's prerequisite
        from .solvers import check_layer_requirements
        check_layer_requirements(merged, merged.deps_extra, self.solvers)

        workdir = self.solve_dir / merged_hash.split(":")[-1][:16]
        if parent_env is not None and parent_env.get("native_lock"):
            # the parent's OWN lock as a local channel: the exact-pin
            # solve no longer depends on the parent's builds surviving
            # in live repodata (bioconda rotates builds away — every
            # published pack had a shelf life of weeks; aba2's
            # "isolated env as a real delta over the base" was blocked
            # on exactly this). Solve-time only; the lock rewrite below
            # erases it from the result.
            from .lock import synth_parent_channel
            workdir.mkdir(parents=True, exist_ok=True)
            channel_url, synth_url_map = synth_parent_channel(
                parent_env["native_lock"], workdir / "parent-channel")
            extra_channels = [channel_url]
        try:
            result, relaxed = self._solve_forgiving(
                merged, workdir, relax, extra_channels=extra_channels)
        except WeftError as e:
            # the failure exists OUTSIDE the exception too: an event a
            # UI can render even when the caller swallows the raise
            kind = (e.code if str(e.code).startswith("env.solve")
                    else "env.solve_error")
            self.store.emit(
                kind, spec=merged_name, code=e.code,
                solve_dir=str(workdir),
                tail=str(e.hints.get("solver_message")
                         or e.hints.get("stderr_tail") or "")[-800:])
            # the hint's MOTIVATING scenario is this failure: a
            # bioconductor-* spec without bioconda essentially always
            # fails to solve — the first wiring attached channel_hint
            # only on success/cached returns, where the problem it warns
            # about had not occurred (consumer audit, 2026-08-24)
            hint = self._channel_hint(merged)
            if hint:
                e.hints = dict(e.hints or {})
                e.hints.setdefault("channel_hint", hint)
            if parent_env is None or e.code != "env.solve_conflict":
                raise
            # the delta cannot be satisfied with the base frozen: that IS the
            # signal to free-solve (and give up the overlay), and the agent
            # should make that call, not us
            solver_msg = e.hints.get("solver_message", "")
            if "No Python interpreter" in str(solver_msg):
                # not a frozen-base problem at all: the parent has no python
                # and pypi deltas need one — ADDING a package is always
                # allowed under extends_env
                suggestion = ("the parent env has no python, which the pypi "
                              "delta needs — add \"python\" to the delta's "
                              "deps.conda (adding new packages never "
                              "conflicts with the frozen base; note a conda "
                              "delta realizes as a full prefix, not an "
                              "overlay)")
            else:
                # ONE owner for the extends_env door (the four-copies
                # incident): the registry discriminates adopt-only
                from .remedies import move_base as _move_base
                suggestion = _move_base(
                    self._lookup_spec(parent_env["spec_hash"])
                    is not None)
            hints = {
                "parent": merged.extends_env,
                "delta": merged.conda + merged.pypi
                + [d for deps in merged.deps_extra.values() for d in deps],
                "solver_message": solver_msg,
                "suggestion": suggestion,
            }
            if not parent_env.get("native_lock") and \
                    "no candidates" in str(solver_msg).lower():
                # legacy env row (pre-native_lock): the failing exact
                # pin is likely BUILD ROTATION (channels prune old
                # builds), which the synthesized parent channel would
                # have absorbed — but there is no lock to synthesize
                # from. The remedy DISCRIMINATES by deployment shape:
                # "re-ensure the parent's spec" costs a full solve on
                # the user's clock — the exact cost adoption exists to
                # avoid — and needs a spec body an adopt-only workspace
                # may not have (consumer report 2026-08-25)
                if self._lookup_spec(parent_env["spec_hash"]) is not None:
                    hints["rotation"] = (
                        "the parent env row predates stored locks, so "
                        "its exact builds must still exist on live "
                        "channels — a rotated-away build fails exactly "
                        "like this. Re-ensure the parent's spec to mint "
                        "a lock-carrying row, then extend that.")
                else:
                    hints["rotation"] = (
                        "the parent env row predates stored locks AND "
                        "carries no spec body (adopted from an older "
                        "tree) — its exact builds must still exist on "
                        "live channels. Re-adopt from a republished "
                        "tree (which carries the lock), or "
                        "bundle_import the env from a workspace that "
                        "holds it.")
            hint = self._channel_hint(merged)
            if hint:                # the failure IS the hint's scenario
                hints["channel_hint"] = hint
            raise WeftError(
                "env.layer_conflict",
                "the delta does not fit on this parent without moving base "
                "package versions",
                stage="solve", hints=hints,
            ) from e
        if synth_url_map:
            # packages the solver took from the synthesized channel point
            # at file:// — a remote realize cannot fetch that. Rewrite to
            # the parent's recorded real URLs (pure pointer fix: same
            # filename, same sha — identity is content and the content is
            # the parent's; conformance-pinned in test_extends_rotation)
            from .lock import rewrite_lock_urls
            result.native_lock = rewrite_lock_urls(
                result.native_lock, synth_url_map,
                synth_root=workdir / "parent-channel")
            if "parent-channel" in result.native_lock:
                surv = [ln.strip()[:120] for ln in
                        result.native_lock.splitlines()
                        if "parent-channel" in ln][:4]
                raise WeftError(
                    "internal.error",
                    "synth-channel URL survived the lock rewrite — the "
                    "child lock would be unrealizable off-controller",
                    stage="solve",
                    hints={"parent": merged.extends_env,
                           "surviving_lines": surv})
        soft_hash = None
        if relaxed:
            # the relaxed spec is what actually got solved — store it as the
            # identity (the lock is exact; adaptiveness was in the *path*).
            # The ORIGINAL soft spec aliases to the same env, so re-ensuring
            # it is a cache hit, not another conflict-relax-solve cycle
            soft_hash = merged_hash
            merged_hash = merged.spec_hash()
            merged_body, merged_name = merged.to_dict(), merged.name
        canonical = result.canonical
        layer_summaries = {}
        conda_pkgs = _conda_provided(canonical)
        for eco, deps in sorted(merged.deps_extra.items()):
            layer = self.solvers[eco].solve(deps, merged, workdir / eco,
                                            conda_packages=conda_pkgs)
            canonical.setdefault("layers", {})[eco] = layer
            layer_summaries[eco] = {
                "packages": len(layer.get("records", [])),
                "from_source": layer.get("from_source", []),
            }
        if canonical.get("url_pins"):
            self._carry_url_pins(merged, canonical)
        from .ids import env_id as compute_env_id
        eid = compute_env_id(canonical)

        if dry_run:
            out = {"env_id": eid, "status": "dry-run (not stored)",
                   "layers": layer_summaries,
                   "summary": {"packages_per_platform": {
                       p: len(v) for p, v in canonical["platforms"].items()}}}
            if relaxed:
                out["relaxed"] = relaxed
            return out

        self.store.put_spec(spec.spec_hash(), spec.name, spec.to_dict())
        self.store.put_spec(merged_hash, merged_name, merged_body)
        self.store.put_env(
            eid, merged_hash, canonical, result.native_lock,
            result.manifest, result.platforms,
            weakly_reproducible=merged.weakly_reproducible(),
        )
        if soft_hash:
            self.store.put_spec_alias(soft_hash, eid)
        out = {"env_id": eid, "status": "solved",
               "summary": self._summary(self.store.get_env(eid))}
        hint = self._channel_hint(merged)   # post-pin: inherited
        if hint:                            # channels count as present
            out["channel_hint"] = hint
        if layer_summaries:
            out["layers"] = layer_summaries
        if parent_env:
            from .overlay import classify_delta
            delta = classify_delta(parent_env["canonical"], canonical)
            self.store.set_env_parent(eid, merged.extends_env,
                                      layerable=delta["layerable"])
            out["extends_env"] = merged.extends_env
            out["delta"] = delta
            out["note"] = (
                "solved against the parent's frozen resolution: the base is "
                "unchanged, so this can realize as an O(delta) overlay on "
                "the parent's prefix"
                if delta["layerable"] else
                "solved against the parent's frozen resolution, but the delta "
                "touches the conda layer, so it realizes as a full prefix "
                f"({delta['why']})")
        if relaxed:
            # transparent: what weft gave up to get you a working env
            out["relaxed"] = relaxed
            out["note"] = ("solved by relaxing soft constraints (see "
                           "`relaxed`); the result is still fully pinned")
            self.store.emit("env.relaxed", env_id=eid, relaxed=relaxed)
        return out

    def _carry_url_pins(self, spec, canonical: dict) -> None:
        """CAS-carry the direct-reference wheels at ENSURE time: the
        CONTROLLER is the only host with a guaranteed egress posture —
        a site never fetches a URL (air-gapped nodes are the point of
        the whole lane). Idempotent by content: a blob already in the
        CAS under its sha is a no-op, so re-ensures and identical pins
        across specs cost nothing. Fail-fast here beats a realize-time
        surprise: a wrong URL or a drifted artifact refuses BEFORE an
        EnvID is minted."""
        import urllib.error
        import urllib.request

        from .ids import sha256_bytes
        from .spec import parse_direct_ref
        if self.cas is None:
            raise WeftError(
                "task.invalid",
                "url pins need the workspace CAS and this EnvManager "
                "was built without one", stage="solve",
                hints={"pins": [p["name"]
                                for p in canonical["url_pins"]]})
        urls = {r["name"]: r["url"]
                for d in spec.pypi
                if (r := parse_direct_ref(d)) is not None}
        for pin in canonical["url_pins"]:
            if self.cas._blob_path(pin["sha256"]).exists():
                continue
            url = urls[pin["name"]]
            try:
                if url.startswith("file://"):
                    data = Path(url[len("file://"):]).read_bytes()
                else:
                    req = urllib.request.Request(
                        url, headers={"User-Agent": "weft"})
                    with urllib.request.urlopen(req, timeout=120) as r:
                        data = r.read(70 * 1024 * 1024)
            except (OSError, urllib.error.URLError) as e:
                raise WeftError(
                    "data.transfer_failed",
                    f"could not fetch url pin {pin['name']!r} from "
                    f"{url}: {e}", stage="solve", retryable=True,
                    hints={"package": pin["name"], "url": url}) from e
            if len(data) >= 64 * 1024 * 1024:
                raise WeftError(
                    "task.invalid",
                    f"url pin {pin['name']!r} is >=64MB — above the "
                    "CAS plain-hash threshold; large-artifact pins "
                    "are a v2 boundary (file an ask)",
                    stage="solve", hints={"package": pin["name"]})
            got = sha256_bytes(data)
            if got != pin["sha256"]:
                raise WeftError(
                    "data.verify_failed",
                    f"url pin {pin['name']!r}: fetched content does "
                    f"not match the declared sha256",
                    stage="solve",
                    hints={"package": pin["name"], "url": url,
                           "expected": pin["sha256"], "got": got,
                           "suggestion": "wrong URL/version, or the "
                                         "artifact changed under the "
                                         "URL — recompute the digest "
                                         "and update the pin"})
            self.cas.put_bytes(data)

    def _summary(self, row: dict) -> dict:
        from .grade import grade_env
        counts = {
            plat: len(pkgs) for plat, pkgs in row["canonical"]["platforms"].items()
        }
        g = grade_env(row["canonical"])
        spec = self.store.get_spec(row["spec_hash"]) or {}
        out = {
            "name": spec.get("name"),
            "packages_per_platform": counts,
            "platforms": row["platforms"],
            # .get: canonical rows can arrive through bundle_import /
            # publish-adopt sidecars, which do not shape-check extras —
            # rendering a sparse-but-accepted record must not crash
            # (the overlay merge at extras= above already treats it as
            # optional; a hard index here was the second, contradictory
            # implementation of that vocabulary)
            "modules": (row["canonical"].get("extras") or {}).get(
                "modules", []),
            # graded confidence, with the soft component identified
            "reproducibility": g["grade"],
            "reproducibility_meaning": g["meaning"],
            "reproducibility_components": g["components"],
            # kept for compatibility; the grade is the richer signal
            "weakly_reproducible": row["weakly_reproducible"],
        }
        if spec.get("notes") or spec.get("step_notes"):
            out["notes"] = spec.get("notes") or []
            out["step_notes"] = spec.get("step_notes") or {}
        return out

    def status(self, env_id: str) -> dict:
        row = self.store.get_env(env_id)
        if not row:
            raise WeftError("task.invalid", f"unknown EnvID: {env_id}", stage="solve")
        realizations = []
        import time as _t
        for r in self.store.realizations_for(env_id):
            entry = {k: r[k] for k in ("site", "strategy", "state", "location")}
            # adopted-RO (institutional tree) vs privately built: a host
            # renders these differently and only one is GC-managed
            entry["read_only"] = bool(r["read_only"])
            # footprint + recency: the LRU/quota metadata a host policy needs
            entry["bytes"] = r["bytes"]
            entry["last_used"] = r["last_used"]
            if r["last_used"]:
                entry["idle_days"] = round(
                    (_t.time() - r["last_used"]) / 86400, 1)
            if r["state"] == "failed" and r.get("log"):
                entry["log_tail"] = r["log"][-800:]  # the probe, right here
            realizations.append(entry)
        return {
            "env_id": env_id,
            "summary": self._summary(row),
            "realizations": realizations,
        }

    # -- adaptive re-materialization -------------------------------------------

    def revise(self, env_id: str, reason: str = "") -> dict:
        """Reproduce-else-revise: when an EnvID can no longer be realized as
        recorded (a package was pulled, a snapshot moved, a tarball 404s),
        re-solve the ORIGINAL SPEC fresh and report the delta.

        This mints a NEW EnvID — it never silently redefines the old one, so
        the content-addressed cache stays sound and memoization stays honest
        (a different env → a different task_hash → no false cache hit)."""
        old = self.store.get_env(env_id)
        if not old:
            raise WeftError("task.invalid", f"unknown EnvID: {env_id}",
                            stage="solve")
        spec_body = self.store.get_spec(old["spec_hash"])
        if not spec_body:
            # a row without a spec body is the ADOPTED/legacy shape —
            # "re-ensure from the original spec" is a shut door there
            from .remedies import revise_no_spec
            raise WeftError(
                "task.invalid",
                f"no spec recorded for {env_id} — cannot revise",
                stage="solve",
                hints={"suggestion": revise_no_spec(adopted=True)})
        # solve fresh from the spec — and keep the solver's OWN output: the
        # stored row is exactly what we suspect is stale, so reading it back
        # would defeat the point (put_env is insert-or-ignore by design)
        merged = resolve_extends(EnvSpec.from_dict(spec_body),
                                 self._lookup_spec)
        parent_env = None
        if merged.extends_env:
            # the spec froze the base to the parent's resolution; a revise
            # must honor that or it mints a child with the parent AMPUTATED
            parent_env = self.store.get_env(merged.extends_env)
            if not parent_env:
                raise WeftError(
                    "task.invalid",
                    f"cannot revise {env_id}: its parent "
                    f"{merged.extends_env} is unknown here", stage="solve")
            merged = self._pin_to_parent(merged, parent_env)
        workdir = self.solve_dir / merged.spec_hash().split(":")[-1][:16]
        try:
            result = solve(merged, workdir, self.pixi_bin)
        except WeftError as e:
            if parent_env is None or e.code != "env.solve_conflict":
                raise
            # ONE owner for the extends_env door (fourth copy of the
            # shut-door incident lived here)
            from .remedies import move_base as _move_base
            free = _move_base(
                self._lookup_spec(parent_env["spec_hash"]) is not None)
            raise WeftError(
                "env.layer_conflict",
                "revise cannot keep the base frozen: the parent's pinned "
                "set no longer solves", stage="solve",
                hints={"parent": merged.extends_env,
                       "solver_message": e.hints.get("solver_message", ""),
                       "suggestion": "revise the parent first, then "
                                     "re-ensure this child on the revised "
                                     f"parent. {free}"})
        canonical = result.canonical
        conda_pkgs = _conda_provided(canonical)
        for eco, deps in sorted(merged.deps_extra.items()):
            canonical.setdefault("layers", {})[eco] = \
                self.solvers[eco].solve(deps, merged, workdir / eco,
                                        conda_packages=conda_pkgs)
        from .ids import env_id as compute_env_id
        new_id = compute_env_id(canonical)

        if new_id == env_id:
            # reproduce: a fresh solve yields the SAME identity, so the
            # recorded lock was stale/corrupt, not the world. Re-derive it
            # and carry on — identity untouched, nothing to report but the fix.
            self.store.replace_env_lock(env_id, result.native_lock,
                                        result.manifest)
            # clear the failed realizations, or the fix looks applied while
            # nothing rebuilds (live-agent eval finding)
            cleared = []
            for r in self.store.realizations_for(env_id):
                if r["state"] in ("failed", "missing"):
                    self.store.set_realization(env_id, r["site"], r["strategy"],
                                               r["location"], "missing",
                                               log="lock re-derived; will rebuild")
                    cleared.append(r["site"])
            self.store.emit("env.restored", env_id=env_id, reason=reason[:200])
            return {"env_id": env_id, "status": "restored",
                    "cleared_realizations": cleared,
                    "note": "a fresh solve reproduces this env exactly; the "
                            "recorded lock was re-derived and failed "
                            "realizations were cleared — the next task using "
                            "this env rebuilds it (pass force=True to re-run a "
                            "task whose result was already memoized)"}
        self.store.put_env(
            new_id, merged.spec_hash(), canonical, result.native_lock,
            result.manifest, result.platforms,
            weakly_reproducible=merged.weakly_reproducible())
        if parent_env:
            from .overlay import classify_delta
            delta = classify_delta(parent_env["canonical"], canonical)
            self.store.set_env_parent(new_id, merged.extends_env,
                                      layerable=delta["layerable"])
        fresh = {"env_id": new_id, "status": "solved",
                 "summary": self._summary(self.store.get_env(new_id))}
        diff = diff_envs(old["canonical"],
                         self.store.get_env(new_id)["canonical"])
        self.store.emit("env.revised", env_id=new_id, revised_from=env_id,
                        changed=len(diff["changed"]),
                        added=len(diff["added"]), removed=len(diff["removed"]),
                        reason=reason[:200])
        return {**fresh, "status": "revised", "revised_from": env_id,
                "diff": diff, "reason": reason,
                "note": "a fresh solve of the same spec produced a DIFFERENT "
                        "package set (see diff); the old EnvID remains valid "
                        "as a record, this one is what will run"}

    def find_near(self, spec_body: dict, site: str | None = None,
                  limit: int = 5) -> list[dict]:
        """Which already-solved (ideally already-realized) envs are close to
        this spec? A QUERY, not a policy: weft never silently substitutes a
        near-match — the agent sees the diff and decides."""
        target = resolve_extends(EnvSpec.from_dict(spec_body),
                                 self._lookup_spec)
        want = {}
        variant_deps = [d for v in (target.variants or {}).values()
                        for k in ("conda", "pypi")
                        for d in (v.get(k) or [])]
        for dep in target.conda + target.pypi + variant_deps:
            from .spec import split_constraint
            n, c = split_constraint(dep)
            want[n] = c
        for eco, deps in target.deps_extra.items():
            for dep in deps:
                want[_layer_dep_name(dep)] = "*"
        if not want:
            return []     # nothing asked for = nothing is "near"
        out = []
        for row in self.store.list_envs():
            env = self.store.get_env(row["env_id"])
            names = {p["name"]: p["version"]
                     for plat in env["canonical"]["platforms"].values()
                     for p in plat}
            for layer in (env["canonical"].get("layers") or {}).values():
                names.update({r["name"]: r["version"]
                              for r in layer.get("records", [])})
            missing = [n for n in want if n not in names]
            # a present name at an unsatisfying version is NOT a match —
            # python 3.9 for a "python =3.13" ask is the decision the agent
            # needs to see, not a distance-0 "perfect hit"
            mismatched = [{"package": n, "have": names[n], "want": c}
                          for n, c in want.items()
                          if n in names and c != "*"
                          and not _satisfies(names[n], c)]
            if len(missing) > len(want) / 2:
                continue      # not remotely the same environment (a version
                              # MISMATCH still ranks — it is the "near" in
                              # near-match; absence is what disqualifies)
            realized = [r["site"] for r in
                        self.store.realizations_for(row["env_id"])
                        if r["state"] == "ready"
                        and (site is None or r["site"] == site)]
            if site is not None and not realized:
                continue
            from .grade import grade_env
            out.append({
                "env_id": row["env_id"],
                "realized_at": realized,
                "missing_packages": missing,
                "version_mismatches": mismatched,
                "distance": len(missing) + len(mismatched),
                "grade": grade_env(env["canonical"])["grade"],
            })
        out.sort(key=lambda e: (e["distance"], not e["realized_at"]))
        return out[:limit]

    def extras(self, env_id: str) -> dict:
        row = self.store.get_env(env_id)
        if not row:
            raise WeftError("task.invalid", f"unknown EnvID: {env_id}", stage="solve")
        return row["canonical"].get("extras") or {}
