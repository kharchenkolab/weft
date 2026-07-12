"""Overlay (stacked) realization: realize a child env in O(delta) by
reusing an already-realized parent's prefix.

The identity model does not move: the child's EnvID is still the hash of
its complete resolved package set, and a site that cannot overlay realizes
the same EnvID as a full prefix, byte-identically. Overlay is purely a
*realization strategy*.

Eligibility — deliberately conservative:

  * the child must have been solved with `extends_env` (so its lock is a
    superset of the parent's BY CONSTRUCTION — no base drift);
  * the delta must live only in LANGUAGE layers (cran / julia / pypi).
    A conda delta is NOT overlaid: conda packages carry embedded prefixes
    and $ORIGIN-relative RPATHs, so a compiled package in a sibling
    directory looks for its libraries in its own dir, not the parent's —
    that fails intermittently at runtime, the worst failure mode there is.
    R libs, Julia projects and Python wheels are *designed* to compose via
    search paths; conda prefixes are not.

Anything ineligible falls back to a full prefix, which is correct and
still cheap in bytes (pixi hardlinks from the shared package cache).
"""

from __future__ import annotations

LAYERABLE_ECOSYSTEMS = ("cran", "julia", "pypi")


def _conda_set(canonical: dict) -> set[tuple]:
    return {
        (p["name"], p["version"], p["build"])
        for plat in canonical.get("platforms", {}).values()
        for p in plat if p["kind"] == "conda"
    }


def _pypi_map(canonical: dict) -> dict[str, str]:
    return {
        p["name"]: p["version"]
        for plat in canonical.get("platforms", {}).values()
        for p in plat if p["kind"] == "pypi"
    }


def _layer_map(canonical: dict, eco: str) -> dict[str, str]:
    layer = (canonical.get("layers") or {}).get(eco) or {}
    return {r["name"]: r["version"] for r in layer.get("records", [])}


def classify_delta(parent_canonical: dict, child_canonical: dict) -> dict:
    """What does the child add, and can it be overlaid?"""
    p_conda, c_conda = _conda_set(parent_canonical), _conda_set(child_canonical)
    conda_added = sorted(n for (n, _, _) in c_conda - p_conda)
    conda_removed = sorted(n for (n, _, _) in p_conda - c_conda)

    p_pypi, c_pypi = _pypi_map(parent_canonical), _pypi_map(child_canonical)
    pypi_added = sorted(set(c_pypi) - set(p_pypi))
    pypi_changed = sorted(n for n in set(c_pypi) & set(p_pypi)
                          if c_pypi[n] != p_pypi[n])

    layers: dict[str, list[str]] = {}
    layer_changed: list[str] = []
    for eco in ("cran", "julia"):
        pm, cm = _layer_map(parent_canonical, eco), _layer_map(child_canonical, eco)
        added = sorted(set(cm) - set(pm))
        if added:
            layers[eco] = added
        layer_changed += [f"{eco}:{n}" for n in set(cm) & set(pm)
                          if cm[n] != pm[n]]

    out = {
        "conda_added": conda_added, "conda_removed": conda_removed,
        "pypi_added": pypi_added, "pypi_changed": pypi_changed,
        "layers_added": layers,
    }
    if conda_removed or pypi_changed or layer_changed:
        out["layerable"] = False
        out["why"] = ("the child changes packages the parent already has "
                      f"({conda_removed + pypi_changed + layer_changed}) — "
                      "that is base drift, and layering it would be incoherent")
        return out
    if conda_added:
        out["layerable"] = False
        out["why"] = (
            f"the delta needs conda package(s) {conda_added}: conda packages "
            "carry embedded prefixes and cannot be safely composed from a "
            "sibling directory. Building a full prefix (cheap in bytes — the "
            "shared package cache is hardlinked). To get the O(delta) path, "
            "put pure-Python deltas in deps.pypi, or add these to the parent.")
        return out
    if not (pypi_added or layers):
        out["layerable"] = False
        out["why"] = "the child adds nothing to the parent"
        return out
    out["layerable"] = True
    out["why"] = "delta lives only in language layers; composable by search path"
    return out
