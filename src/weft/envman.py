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
    def __init__(self, store: Store, solve_dir: Path, pixi_bin: str):
        self.store = store
        self.solve_dir = Path(solve_dir)
        self.pixi_bin = pixi_bin

    def _lookup_spec(self, spec_hash: str) -> EnvSpec | None:
        body = self.store.get_spec(spec_hash)
        return EnvSpec.from_dict(body) if body else None

    def ensure(self, spec_or_id, *, update: bool = False) -> dict:
        """Accepts an EnvID string or a spec dict; returns {env_id, status, summary}."""
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
        self.store.put_spec(spec.spec_hash(), spec.name, spec.to_dict())
        merged = resolve_extends(spec, self._lookup_spec)
        merged_hash = merged.spec_hash()
        self.store.put_spec(merged_hash, merged.name, merged.to_dict())

        if not update:
            cached = self.store.env_for_spec(merged_hash)
            if cached:
                return {"env_id": cached, "status": "cached",
                        "summary": self._summary(self.store.get_env(cached))}

        result = solve(
            merged, self.solve_dir / merged_hash.split(":")[-1][:16], self.pixi_bin
        )
        self.store.put_env(
            result.env_id, merged_hash, result.canonical, result.native_lock,
            result.manifest, result.platforms,
            weakly_reproducible=merged.weakly_reproducible(),
        )
        return {"env_id": result.env_id, "status": "solved",
                "summary": self._summary(self.store.get_env(result.env_id))}

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
        return {
            "env_id": env_id,
            "summary": self._summary(row),
            "realizations": [
                {k: r[k] for k in ("site", "strategy", "state", "location")}
                for r in self.store.realizations_for(env_id)
            ],
        }

    def extras(self, env_id: str) -> dict:
        row = self.store.get_env(env_id)
        if not row:
            raise WeftError("task.invalid", f"unknown EnvID: {env_id}", stage="solve")
        return row["canonical"]["extras"]
