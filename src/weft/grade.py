"""Reproducibility grades: confidence as a spectrum, not a boolean.

The design's stance: weft *grades and reports*; the agent decides. An
adaptive step that unblocks real work is a normal, supported move — weft's
job is to label what it can and cannot pin, so a re-run's risk is visible.

The ladder, worst-rung-wins, with a per-component breakdown so the soft
step is identifiable:

  fully-pinned    every package content-hashed (conda/pypi lock)
  snapshot-pinned dated-snapshot / commit-SHA layers (CRAN, GitHub,
                  Julia): reproduces almost always, but the artifact is
                  not content-hashed — an index can withdraw a version
  attested        depends on site `modules` weft cannot pin at all
  escape-hatch    spec used post_install / a session installer
  state-dependent kernel-promoted transcript (replayable, not re-derived)
"""

from __future__ import annotations

LADDER = ["fully-pinned", "snapshot-pinned", "attested", "escape-hatch",
          "state-dependent"]

MEANING = {
    "fully-pinned": "every package is content-hashed; re-materializes exactly",
    "snapshot-pinned": "pinned to dated snapshots / commit SHAs; reproduces "
                       "almost always, but the artifacts are not "
                       "content-hashed (an index can withdraw a version)",
    "attested": "depends on site-provided modules weft cannot pin; the site "
                "attests, weft records",
    "escape-hatch": "an adaptive install step (post_install / session "
                    "installer) ran; its effects are not content-pinned",
    "state-dependent": "produced from accumulated interpreter state; "
                       "replayable from the recorded transcript",
}


def _worst(grades: list[str]) -> str:
    return max(grades, key=LADDER.index) if grades else "fully-pinned"


def grade_env(canonical: dict) -> dict:
    """-> {grade, components: [{component, grade, why}]}"""
    extras = canonical.get("extras", {}) or {}
    components: list[dict] = []

    conda_unhashed = [
        p["name"] for plat in canonical.get("platforms", {}).values()
        for p in plat if not p.get("sha256")
    ]
    components.append({
        "component": "conda/pypi",
        "grade": "fully-pinned" if not conda_unhashed else "snapshot-pinned",
        "why": "all packages content-hashed" if not conda_unhashed
        else f"{len(conda_unhashed)} package(s) without a content hash",
    })

    for eco, layer in (canonical.get("layers") or {}).items():
        recs = layer.get("records", [])
        hashed = [r for r in recs if r.get("sha256")]
        if recs and len(hashed) == len(recs):
            g, why = "fully-pinned", "all layer packages content-hashed"
        else:
            g = "snapshot-pinned"
            why = (f"pinned to {layer['snapshot']}" if layer.get("snapshot")
                   else "pinned by commit/tree hash, not content hash")
        components.append({"component": f"{eco} layer", "grade": g,
                           "why": why})

    if extras.get("modules"):
        components.append({
            "component": "site modules", "grade": "attested",
            "why": f"site-provided: {extras['modules']} (named, not pinned)"})
    if extras.get("post_install"):
        # portability is the question that actually matters: does this
        # rebuild anywhere, or does it secretly depend on one filesystem?
        # EVERY step needs its sources captured — one uncaptured step is
        # one missing filesystem, and "portable" is an all-or-nothing claim
        n_inputs = len(extras.get("post_install_inputs") or [])
        portable = n_inputs >= len(extras["post_install"])
        components.append({
            "component": "post_install", "grade": "escape-hatch",
            "portable": portable,
            "why": f"{len(extras['post_install'])} adaptive install step(s); "
                   + ("sources travel with the env (content-addressed), so it "
                      "rebuilds anywhere — effects still not content-pinned"
                      if portable else
                      "NO post_install_inputs: if a step reads local paths or "
                      "the network, this env may not rebuild elsewhere — "
                      "register the sources and reference them to make it "
                      "portable")})

    grade = _worst([c["grade"] for c in components])
    return {"grade": grade, "meaning": MEANING[grade], "components": components}


def grade_manifest(env_grade: dict | None, transcript: bool = False) -> dict:
    """The grade a *result* carries (a kernel promotion is state-dependent
    regardless of how well-pinned its env was)."""
    if transcript:
        return {"grade": "state-dependent", "meaning": MEANING["state-dependent"],
                "components": (env_grade or {}).get("components", [])
                + [{"component": "execution", "grade": "state-dependent",
                    "why": "promoted from kernel state; replay the transcript "
                           "to re-derive"}]}
    if env_grade is None:
        return {"grade": "attested", "meaning": MEANING["attested"],
                "components": [{"component": "environment", "grade": "attested",
                                "why": "bare site environment — the site's own "
                                       "tools, which weft does not pin"}]}
    return env_grade
