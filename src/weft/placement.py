"""Placement for site:"auto" — filter by capability, rank by cache warmth.

Deliberately simple (doc 01 §5): the ranked list *with reasons* is returned
to the agent; low-confidence choices surface rather than being silently
decided. Every filtered-out site carries its reason too — that is what lets
an agent re-plan instead of guessing.
"""

from __future__ import annotations

from .capability import satisfies_resources, scheduler_type


def rank_sites(
    task_resources: dict,
    env_modules: list[str],
    sites: list[dict],           # store site rows (with capabilities, health)
    env_realized_at: set[str],   # site names where the task env is ready
    data_present: dict[str, int],  # site -> bytes of required refs already there
    total_bytes: int,
    preferences: dict[str, float] | None = None,
) -> dict:
    ranked, rejected = [], []
    for site in sites:
        name = site["name"]
        caps = site.get("capabilities")
        if site.get("health") not in ("ok", None, "unknown") :
            rejected.append({"site": name, "reason": f"health={site['health']}"})
            continue
        if caps is None:
            rejected.append({"site": name, "reason": "never probed"})
            continue
        ok, hints = satisfies_resources(caps, task_resources)
        if not ok:
            rejected.append({"site": name, "reason": "resources", "hints": hints})
            continue
        if env_modules and not caps.get("module_system"):
            rejected.append({"site": name, "reason": "spec needs site modules; none here",
                             "modules": env_modules})
            continue
        score, why = 0.0, []
        if name in env_realized_at:
            score += 3.0
            why.append("environment already realized")
        if total_bytes > 0:
            frac = data_present.get(name, 0) / total_bytes
            score += 3.0 * frac
            if frac > 0:
                why.append(f"{int(frac * 100)}% of input bytes already cached")
        if scheduler_type(caps) == "none":
            score += 1.0
            why.append("interactive (no queue)")
        score += (preferences or {}).get(name, 0.0)
        ranked.append({"site": name, "score": round(score, 2), "why": why})
    ranked.sort(key=lambda r: -r["score"])
    return {
        "ranked": ranked,
        "rejected": rejected,
        "confident": len(ranked) > 0 and (
            len(ranked) == 1 or ranked[0]["score"] - ranked[1]["score"] >= 1.0
        ),
    }
