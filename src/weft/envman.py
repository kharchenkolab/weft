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


def diff_envs(old_canonical: dict, new_canonical: dict) -> dict:
    """Package-level delta between two resolved envs — what an agent (and a
    user) needs to judge whether the near-match is acceptable."""
    def flat(c):
        out = {}
        for plat in c.get("platforms", {}).values():
            for p in plat:
                out[p["name"]] = p["version"]
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
                 solvers: dict | None = None):
        self.store = store
        self.solve_dir = Path(solve_dir)
        self.pixi_bin = pixi_bin
        from .solvers import default_solvers
        self.solvers: dict = {**default_solvers(pixi_bin), **(solvers or {})}

    def _lookup_spec(self, spec_hash: str) -> EnvSpec | None:
        body = self.store.get_spec(spec_hash)
        return EnvSpec.from_dict(body) if body else None

    @staticmethod
    def _pin_to_parent(spec: EnvSpec, parent_env: dict) -> EnvSpec:
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
        plat = spec.platforms[0] if spec.platforms else "linux-64"
        canonical = parent_env["canonical"]

        def resolve(pins: list[str], delta: list[str], kind: str) -> list[str]:
            pinned = {split_constraint(p)[0]: split_constraint(p)[1]
                      for p in pins}
            keep_delta = []
            for dep in delta:
                name, constraint = split_constraint(dep)
                if name not in pinned:
                    keep_delta.append(dep)
                    continue
                version = pinned[name].lstrip("=").split()[0]
                if constraint == "*" or _satisfies(version, constraint):
                    continue      # redundant: the frozen base already has it
                raise WeftError(
                    "env.layer_conflict",
                    f"the delta asks for {kind} {name} {constraint}, but the "
                    f"parent has it pinned at {version}",
                    stage="solve",
                    hints={
                        "parent": spec.extends_env, "package": name,
                        "parent_version": version, "requested": constraint,
                        "suggestion": "`extends_env` freezes the base on "
                                      "purpose. To move it, re-ensure with "
                                      "`extends` (the parent's SPEC hash) for "
                                      "a free re-solve and a full prefix.",
                    })
            return pins + keep_delta

        out.conda = resolve(parent_pins(canonical, plat), spec.conda, "conda")
        out.pypi = resolve(parent_pypi_pins(canonical, plat), spec.pypi, "pypi")
        # the parent's own extras carry over (modules, env_vars, …)
        extras = canonical.get("extras", {})
        out.modules = extras.get("modules") or out.modules
        out.env_vars = {**(extras.get("env_vars") or {}), **out.env_vars}
        # parent's language layers are inherited; the child adds to them
        for eco, layer in (canonical.get("layers") or {}).items():
            parent_deps = layer.get("top_level") or []
            out.deps_extra[eco] = [
                d for d in parent_deps
                if d not in out.deps_extra.get(eco, [])
            ] + out.deps_extra.get(eco, [])
        return out

    def _solve_forgiving(self, merged: EnvSpec, workdir: Path, relax: str):
        """Solve as written; under relax="soft", greedily drop SOFT
        constraints (trailing '?') until it solves. Hard pins are never
        touched — a silent version drop is precisely what a substrate must
        not do. The result is still fully pinned: adaptiveness lives in the
        path to a solve, not in what you got."""
        from .spec import is_soft, relax_dep
        try:
            return solve(merged, workdir, self.pixi_bin), []
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
                relaxed.append({"dep": requested.rstrip("? ").strip(),
                                "ecosystem": eco,
                                "relaxed_to": deps[i]})
                try:
                    result = solve(merged, workdir, self.pixi_bin)
                except WeftError:
                    continue     # still conflicting: relax the next one too
                for r in relaxed:
                    r["got"] = next(
                        (p["version"] for plat in
                         result.canonical["platforms"].values()
                         for p in plat if p["name"] == r["relaxed_to"]), None)
                return result, relaxed
            first.hints["tried_relaxing"] = [r["dep"] for r in relaxed]
            first.hints["suggestion"] = (
                "even with every soft constraint relaxed this does not "
                "solve — a hard pin (or the package set itself) is the "
                "conflict; the solver_message names it")
            raise

    def ensure(self, spec_or_id, *, update: bool = False,
               dry_run: bool = False, relax: str = "none") -> dict:
        """Accepts an EnvID string or a spec dict; returns {env_id, status, summary}.
        dry_run solves everything but stores nothing — cheap fix-testing."""
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
        from .solvers import check_layer_requirements
        check_layer_requirements(merged, merged.deps_extra, self.solvers)

        if not update and not dry_run:
            cached = self.store.env_for_spec(merged_hash)
            if cached:
                return {"env_id": cached, "status": "cached",
                        "summary": self._summary(self.store.get_env(cached))}

        # extends_env: pin the parent's resolution, solve only the delta
        parent_env = None
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

        workdir = self.solve_dir / merged_hash.split(":")[-1][:16]
        try:
            result, relaxed = self._solve_forgiving(merged, workdir, relax)
        except WeftError as e:
            if parent_env is None or e.code != "env.solve_conflict":
                raise
            # the delta cannot be satisfied with the base frozen: that IS the
            # signal to free-solve (and give up the overlay), and the agent
            # should make that call, not us
            raise WeftError(
                "env.layer_conflict",
                "the delta does not fit on this parent without moving base "
                "package versions",
                stage="solve",
                hints={
                    "parent": merged.extends_env,
                    "delta": merged.conda + merged.pypi
                    + [d for deps in merged.deps_extra.values() for d in deps],
                    "solver_message": e.hints.get("solver_message", ""),
                    "suggestion": "re-ensure with `extends` (the parent's SPEC "
                                  "hash) instead of `extends_env`: that frees "
                                  "the base to move, costs a full solve and a "
                                  "full prefix, and is the right call when the "
                                  "delta genuinely needs a newer base",
                },
            ) from e
        if relaxed:
            # the relaxed spec is what actually got solved — store it as the
            # identity (the lock is exact; adaptiveness was in the *path*)
            merged_hash = merged.spec_hash()
        canonical = result.canonical
        layer_summaries = {}
        for eco, deps in sorted(merged.deps_extra.items()):
            layer = self.solvers[eco].solve(deps, merged, workdir / eco)
            canonical.setdefault("layers", {})[eco] = layer
            layer_summaries[eco] = {
                "packages": len(layer.get("records", [])),
                "from_source": layer.get("from_source", []),
            }
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
        self.store.put_spec(merged_hash, merged.name, merged.to_dict())
        self.store.put_env(
            eid, merged_hash, canonical, result.native_lock,
            result.manifest, result.platforms,
            weakly_reproducible=merged.weakly_reproducible(),
        )
        out = {"env_id": eid, "status": "solved",
               "summary": self._summary(self.store.get_env(eid))}
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

    def _summary(self, row: dict) -> dict:
        from .grade import grade_env
        counts = {
            plat: len(pkgs) for plat, pkgs in row["canonical"]["platforms"].items()
        }
        g = grade_env(row["canonical"])
        spec = self.store.get_spec(row["spec_hash"]) or {}
        out = {
            "packages_per_platform": counts,
            "platforms": row["platforms"],
            "modules": row["canonical"]["extras"]["modules"],
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
            raise WeftError(
                "task.invalid",
                f"no spec recorded for {env_id} — cannot revise",
                stage="solve",
                hints={"suggestion": "re-ensure from the original spec"})
        # solve fresh from the spec — and keep the solver's OWN output: the
        # stored row is exactly what we suspect is stale, so reading it back
        # would defeat the point (put_env is insert-or-ignore by design)
        merged = resolve_extends(EnvSpec.from_dict(spec_body),
                                 self._lookup_spec)
        workdir = self.solve_dir / merged.spec_hash().split(":")[-1][:16]
        result = solve(merged, workdir, self.pixi_bin)
        canonical = result.canonical
        for eco, deps in sorted(merged.deps_extra.items()):
            canonical.setdefault("layers", {})[eco] = \
                self.solvers[eco].solve(deps, merged, workdir / eco)
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
        for dep in target.conda + target.pypi:
            from .spec import split_constraint
            n, c = split_constraint(dep)
            want[n] = c
        out = []
        for row in self.store.list_envs():
            env = self.store.get_env(row["env_id"])
            names = {p["name"]: p["version"]
                     for plat in env["canonical"]["platforms"].values()
                     for p in plat}
            missing = [n for n in want if n not in names]
            if len(missing) > len(want) / 2:
                continue      # not remotely the same environment
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
                "distance": len(missing),
                "grade": grade_env(env["canonical"])["grade"],
            })
        out.sort(key=lambda e: (e["distance"], not e["realized_at"]))
        return out[:limit]

    def extras(self, env_id: str) -> dict:
        row = self.store.get_env(env_id)
        if not row:
            raise WeftError("task.invalid", f"unknown EnvID: {env_id}", stage="solve")
        return row["canonical"]["extras"]
