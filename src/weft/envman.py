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

    def ensure(self, spec_or_id, *, update: bool = False,
               dry_run: bool = False) -> dict:
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

        workdir = self.solve_dir / merged_hash.split(":")[-1][:16]
        result = solve(merged, workdir, self.pixi_bin)
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
            return {"env_id": eid, "status": "dry-run (not stored)",
                    "layers": layer_summaries,
                    "summary": {"packages_per_platform": {
                        p: len(v) for p, v in canonical["platforms"].items()}}}

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
        return out

    def _summary(self, row: dict) -> dict:
        counts = {
            plat: len(pkgs) for plat, pkgs in row["canonical"]["platforms"].items()
        }
        return {
            "packages_per_platform": counts,
            "platforms": row["platforms"],
            "modules": row["canonical"]["extras"]["modules"],
            "weakly_reproducible": row["weakly_reproducible"],
        }

    def status(self, env_id: str) -> dict:
        row = self.store.get_env(env_id)
        if not row:
            raise WeftError("task.invalid", f"unknown EnvID: {env_id}", stage="solve")
        realizations = []
        for r in self.store.realizations_for(env_id):
            entry = {k: r[k] for k in ("site", "strategy", "state", "location")}
            if r["state"] == "failed" and r.get("log"):
                entry["log_tail"] = r["log"][-800:]  # the probe, right here
            realizations.append(entry)
        return {
            "env_id": env_id,
            "summary": self._summary(row),
            "realizations": realizations,
        }

    def extras(self, env_id: str) -> dict:
        row = self.store.get_env(env_id)
        if not row:
            raise WeftError("task.invalid", f"unknown EnvID: {env_id}", stage="solve")
        return row["canonical"]["extras"]
