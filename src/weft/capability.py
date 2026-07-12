"""Capability records: normalized site facts that drive every decision.

A record has a top-level (login/direct) view and an optional `compute`
sub-record for scheduler sites where compute nodes differ from the login
node. Accessors below always answer for "where jobs actually run".
"""

from __future__ import annotations


def normalize_probe(probe: dict, compute_probe: dict | None = None) -> dict:
    import time
    caps = {
        # v2: versioned; carries where each record was MEASURED (login vs
        # a compute node via probe-job) and when — a capability without
        # provenance is a guess wearing a fact's clothes
        "schema": "capabilities:v2",
        "probed_at": time.time(),
        "measured_on": probe.get("hostname", "") or "login",
        "os": probe.get("os", "linux"),
        "arch": probe.get("arch", "x86_64"),
        "hostname": probe.get("hostname", ""),
        "cpus": int(probe.get("cpus", 1)),
        "mem_gb": int(probe.get("mem_gb", 0)),
        "glibc": probe.get("glibc", ""),
        "internet": bool(probe.get("internet", False)),
        "runtimes": probe.get("runtimes", {}),
        "scheduler": probe.get("scheduler", {"type": "none"}),
        "module_system": bool(probe.get("module_system", False)),
        "gpus": probe.get("gpus", []),
        "cuda_driver": probe.get("cuda_driver", ""),
        "storage": probe.get("storage", {}),
        "shim_version": probe.get("shim_version"),
    }
    if compute_probe:
        caps["compute"] = normalize_probe(compute_probe)
    return caps


def compute_view(caps: dict) -> dict:
    """The record for the nodes that execute jobs."""
    return caps.get("compute") or caps


def scheduler_type(caps: dict) -> str:
    return (caps.get("scheduler") or {}).get("type", "none")


def has_apptainer(caps: dict) -> bool:
    v = compute_view(caps).get("runtimes", {}).get("apptainer", "")
    return bool(v)


def gpu_count(caps: dict) -> int:
    return sum(int(g.get("count", 0)) for g in compute_view(caps).get("gpus", []))


def slurm_time_to_s(t: str) -> float | None:
    """'1-00:00:00', '4:00:00', '30:00', 'infinite' -> seconds (None = no limit)."""
    t = (t or "").strip().lower()
    if not t or t in ("infinite", "unlimited", "n/a"):
        return None
    days = 0
    if "-" in t:
        d, t = t.split("-", 1)
        days = int(d)
    parts = [int(p) for p in t.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3:]
    return days * 86400 + h * 3600 + m * 60 + s


def satisfies_resources(caps: dict, resources: dict, partitions: list[dict] | None = None) -> tuple[bool, dict]:
    """Check a resource ask against capabilities.

    Returns (ok, violation_hints). Hints carry the nearest valid ask so the
    agent can right-size instead of guessing (doc 05 §3).
    """
    view = compute_view(caps)
    hints: dict = {}
    ok = True
    if resources.get("cpus", 1) > view.get("cpus", 1):
        ok = False
        hints["cpus"] = {"asked": resources["cpus"], "max": view.get("cpus", 1)}
    if resources.get("mem_gb", 0) > view.get("mem_gb", 0) > 0:
        ok = False
        hints["mem_gb"] = {"asked": resources["mem_gb"], "max": view.get("mem_gb")}
    max_gpus = gpu_count(caps)
    if partitions:
        # scheduler sites: GPUs live on compute nodes the login probe
        # cannot see — partition GRES is the authoritative count there
        max_gpus = max([max_gpus] + [
            sum(g.get("count", 0) for g in (p.get("gres") or [])
                if g.get("type") == "gpu")
            for p in partitions])
    if resources.get("gpus", 0) > max_gpus:
        ok = False
        hints["gpus"] = {"asked": resources["gpus"], "max": max_gpus}
    if partitions:
        # a scheduler ask must fit at least one partition (cpus, mem, walltime)
        asked_wall = slurm_time_to_s(resources.get("walltime", ""))
        fits = []
        for p in partitions:
            fit = resources.get("cpus", 1) <= p.get("cpus_per_node", 10**9) and \
                  resources.get("mem_gb", 0) <= p.get("mem_gb_per_node", 10**9)
            pmax = slurm_time_to_s(p.get("max_walltime", ""))
            if fit and asked_wall and pmax is not None and asked_wall > pmax:
                fit = False
            if fit and resources.get("gpus", 0):
                pg = sum(g.get("count", 0) for g in (p.get("gres") or [])
                         if g.get("type") == "gpu")
                if resources["gpus"] > pg:
                    fit = False
            if fit:
                fits.append(p["name"])
        if not fits:
            ok = False
            hints["partitions"] = {
                "asked": {k: resources.get(k)
                          for k in ("cpus", "mem_gb", "walltime")},
                "available": partitions,
                "suggestion": "shrink the ask to fit a partition, or pick "
                              "another site",
            }
        else:
            hints["fitting_partitions"] = fits
    return ok, hints
