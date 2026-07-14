"""Realization strategy selection: a pure decision table over capabilities.

    compute internet + storage shared with install point  -> prefix
    parallel-FS root (BeeGFS/Lustre/GPFS) + squashfs-able  -> squashfs
    no internet, apptainer present                         -> container
    no internet, no container runtime                      -> packed
    spec declares modules                                  -> modules+<base>
    spec declares container_base                           -> container required

squashfs (one mounted image instead of ~100k files on the shared FS) is
auto-selected only where the per-file metadata cost recurs — a parallel
filesystem root; elsewhere it is opt-in via prefer ("squashfs" — e.g.
NFS-hosted institutional read-only envs want it too). Requires
squashfs_mode(caps) (fuse device + squashfuse + mksquashfs on site, and
either userns or a mountable-over root fs).

The table is a pure function so it is exhaustively unit-testable, and the
agent may override with an explicit request when diagnosing site quirks
(the override is validated, not blindly obeyed).
"""

from __future__ import annotations

from .capability import PARALLEL_FS, compute_view, has_apptainer, squashfs_mode
from .errors import WeftError

KNOWN = ("prefix", "packed", "container", "squashfs",
         "modules+prefix", "modules+packed", "modules+squashfs")


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
        if base == "squashfs" and squashfs_mode(caps) is None:
            raise WeftError(
                "env.unsatisfiable_on_site",
                "squashfs strategy requested but the site cannot mount or "
                "build images",
                stage="realize",
                hints={"squashfs": view.get("squashfs") or {},
                       "needs": "fuse device + squashfuse + mksquashfs, and "
                                "userns or a root fs fusermount may mount "
                                "over (not a parallel FS)",
                       "alternatives": ["prefix", "packed"]},
            )
        return prefer

    if container_base:
        if modules:
            # inherently contradictory spec, regardless of site
            raise WeftError(
                "task.invalid",
                "modules and container_base cannot combine (site modules are "
                "not visible inside containers)",
                stage="realize",
            )
        if not (apptainer or docker):
            raise WeftError(
                "env.unsatisfiable_on_site",
                "spec sets container_base but site has no container runtime",
                stage="realize",
                hints={"runtimes": view.get("runtimes", {})},
            )
        return "container"

    # parallel-FS root: every interpreter start pays per-file metadata on
    # the shared FS forever — one mounted image is the honest default
    # where the site can build and mount it (needs internet OR a packed
    # blob to build from; the builder handles both, so gate on tooling)
    root_fs = (view.get("squashfs") or {}).get("root_fs") or ""
    if root_fs in PARALLEL_FS and squashfs_mode(caps):
        return "modules+squashfs" if modules else "squashfs"

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
