"""Realization strategy selection: a pure decision table over capabilities.

    compute internet + storage shared with install point  -> prefix
    no internet, apptainer present                         -> container
    no internet, no container runtime                      -> packed
    spec declares modules                                  -> modules+<base>
    spec declares container_base                           -> container required

The table is a pure function so it is exhaustively unit-testable, and the
agent may override with an explicit request when diagnosing site quirks
(the override is validated, not blindly obeyed).
"""

from __future__ import annotations

from .capability import compute_view, has_apptainer
from .errors import WeftError

KNOWN = ("prefix", "packed", "container", "modules+prefix", "modules+packed")


def select_strategy(
    caps: dict,
    *,
    modules: list[str] | None = None,
    container_base: str | None = None,
    prefer: str | None = None,
) -> str:
    view = compute_view(caps)
    internet = bool(view.get("internet"))
    apptainer = has_apptainer(caps)
    docker = bool(view.get("runtimes", {}).get("docker"))
    modules = modules or []

    if prefer is not None:
        if prefer not in KNOWN:
            raise WeftError(
                "task.invalid", f"unknown strategy {prefer!r}", stage="realize",
                hints={"known": list(KNOWN)},
            )
        base = prefer.split("+")[-1]
        if base == "container" and not (apptainer or docker):
            raise WeftError(
                "env.unsatisfiable_on_site",
                "container strategy requested but no container runtime on site",
                stage="realize",
                hints={"runtimes": view.get("runtimes", {})},
            )
        if base == "prefix" and not internet:
            raise WeftError(
                "env.unsatisfiable_on_site",
                "prefix strategy needs index access from the install point",
                stage="realize",
                hints={"alternatives": ["packed", "container"]},
            )
        return prefer

    if container_base:
        if not (apptainer or docker):
            raise WeftError(
                "env.unsatisfiable_on_site",
                "spec sets container_base but site has no container runtime",
                stage="realize",
                hints={"runtimes": view.get("runtimes", {})},
            )
        if modules:
            raise WeftError(
                "task.invalid",
                "modules and container_base cannot combine (site modules are "
                "not visible inside containers)",
                stage="realize",
            )
        return "container"

    if internet:
        base = "prefix"
    elif apptainer:
        base = "container"
    else:
        base = "packed"

    if modules:
        if base == "container":
            base = "packed"  # module paths are host paths; keep env on host
        return f"modules+{base}"
    return base
