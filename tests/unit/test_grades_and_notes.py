"""Step 1: graded reproducibility + identity-neutral notes."""

from weft.grade import grade_env, grade_manifest
from weft.spec import EnvSpec


def _canon(**kw):
    base = {
        "version": 1,
        "platforms": {"linux-64": [
            {"kind": "conda", "name": "python", "version": "3.12",
             "build": "h1", "sha256": "aa"},
        ]},
        "extras": {"modules": [], "post_install": [], "container_base": None,
                   "env_vars": {}},
    }
    base["extras"].update(kw.pop("extras", {}))
    base.update(kw)
    return base


def test_fully_pinned_is_the_top_rung():
    g = grade_env(_canon())
    assert g["grade"] == "fully-pinned"
    assert g["components"][0]["component"] == "conda/pypi"


def test_snapshot_layers_are_not_top_rung():
    """The honesty fix: a dated-snapshot CRAN layer is NOT as reproducible
    as a content-hashed conda lock, and must not claim to be."""
    c = _canon(layers={"cran": {"records": [{"name": "jsonlite",
                                             "version": "2.0.1",
                                             "sha256": ""}],
                                "snapshot": "https://ppm/2026-07-01"}})
    g = grade_env(c)
    assert g["grade"] == "snapshot-pinned"
    cran = next(x for x in g["components"] if x["component"] == "cran layer")
    assert "2026-07-01" in cran["why"]


def test_modules_and_post_install_rungs():
    g = grade_env(_canon(extras={"modules": ["espresso/7.2"]}))
    assert g["grade"] == "attested"
    g2 = grade_env(_canon(extras={"modules": ["espresso/7.2"],
                                  "post_install": ["pip install ./vendored"]}))
    assert g2["grade"] == "escape-hatch"          # worst rung wins
    comps = {c["component"]: c["grade"] for c in g2["components"]}
    assert comps["site modules"] == "attested"
    assert comps["post_install"] == "escape-hatch"


def test_manifest_grades():
    env_g = grade_env(_canon())
    assert grade_manifest(env_g)["grade"] == "fully-pinned"
    assert grade_manifest(env_g, transcript=True)["grade"] == "state-dependent"
    bare = grade_manifest(None)
    assert bare["grade"] == "attested"        # bare site tools are unpinned


def test_notes_are_identity_neutral():
    """An agent may explain an adaptive step without forking the EnvID and
    orphaning every cached realization."""
    plain = {"name": "e", "deps": {"conda": ["numpy"]},
             "post_install": ["pip install ./vendored-fix"]}
    annotated = {**plain,
                 "notes": ["upstream wheel is broken on 2.1; vendored patch"],
                 "step_notes": {"0": "remove once upstream ships 2.2"}}
    a, b = EnvSpec.from_dict(plain), EnvSpec.from_dict(annotated)
    assert a.spec_hash() == b.spec_hash()      # identity untouched
    assert b.notes and b.step_notes            # but carried in the body
    assert b.to_dict()["notes"] == annotated["notes"]
    # notes merge across layering
    child = EnvSpec.from_dict({"deps": {}, "notes": ["child rationale"]})
    merged = child.merged_onto(b)
    assert len(merged.notes) == 2
