"""Solver registry: one resolver per packaging ecosystem (design-next §2).

A Solver turns declarative deps into a locked *layer* of the canonical
lock document, and knows how to realize that layer into an existing env
directory on a site. The conda+pypi pair is one layer (pixi solves them
together); further ecosystems (cran, julia, …) stack on top, each
appending its activation lines.

Operability contract (design-next §4): solve failures are WeftError
`env.solve_conflict` with normalized hints {ecosystem, solver_message,
user_pins}; cross-layer requirement violations are `env.layer_conflict`
naming both sides and the fix.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .errors import WeftError


class Solver(Protocol):
    ecosystem: str

    def solve(self, deps: list[str], spec, workdir: Path) -> dict:
        """-> layer dict: {"records": [...sorted, hashed...],
        "native": <native lockfile text>, "requires": {...}}"""
        ...

    def realize_layer(self, layer: dict, adapter, env_rel: str) -> str:
        """Install the layer into the realized env dir on the site;
        return activation lines to append (e.g. R_LIBS exports)."""
        ...

    def why(self, env_row: dict, package: str, workdir: Path) -> str:
        """Reverse-dependency explanation for a package in this layer."""
        ...


class PixiSolver:
    """The conda(+pypi) layer — wraps lock.solve; realization is the
    base strategy's job (prefix/packed), so realize_layer is a no-op."""

    ecosystem = "conda"

    def __init__(self, pixi_bin: str):
        self.pixi_bin = pixi_bin

    def solve(self, deps, spec, workdir):  # handled by lock.solve upstream
        raise NotImplementedError("pixi layer is solved via lock.solve")

    def realize_layer(self, layer, adapter, env_rel):
        return ""

    def why(self, env_row: dict, package: str, workdir: Path) -> str:
        import subprocess
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "pixi.toml").write_text(env_row["manifest"])
        (workdir / "pixi.lock").write_text(env_row["native_lock"])
        r = subprocess.run(
            [self.pixi_bin, "tree", "--manifest-path",
             str(workdir / "pixi.toml"), "--invert", package],
            capture_output=True, text=True, timeout=300,
        )
        out = (r.stdout or r.stderr).strip()
        return out[-3000:] if out else f"{package}: not found in the conda layer"


def check_layer_requirements(spec, layers_present: dict[str, list[str]]) -> None:
    """Cross-layer contract checks, before any solving is paid for."""
    conda_names = {d.split()[0].lower() for d in spec.conda}
    if layers_present.get("cran") and "r-base" not in conda_names:
        raise WeftError(
            "env.layer_conflict",
            "cran layer requires R itself from the conda layer",
            stage="solve",
            hints={"layer": "cran", "needs": "r-base in deps.conda",
                   "have_conda": sorted(conda_names),
                   "suggestion": 'add e.g. "r-base =4.4" (and version-pin it: '
                                 "the cran packages will be built against it)"},
        )
    if layers_present.get("julia") and "julia" not in conda_names:
        raise WeftError(
            "env.layer_conflict",
            "julia layer requires julia itself from the conda layer",
            stage="solve",
            hints={"layer": "julia", "needs": "julia in deps.conda",
                   "suggestion": 'add "julia" to deps.conda'},
        )
