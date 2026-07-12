"""Regressions for the three-reviewer audit of the adaptivity/eviction/
layering rounds (pure-function fixes; the live ones are in
tests/integration/test_review_fixes.py)."""

from weft.envman import _layer_dep_name, _pep503, diff_envs
from weft.grade import grade_env
from weft.overlay import classify_delta
from weft.spec import EnvSpec


def test_portable_requires_every_step_captured():
    """One uncaptured installer step = one missing filesystem: 'portable'
    is an all-or-nothing claim."""
    base = {"platforms": {"linux-64": []}, "extras": {
        "modules": [], "env_vars": {}, "container_base": None,
        "post_install": ["pip install ./a", "pip install ./b"],
        "post_install_inputs": [{"ref": "sha256:aa", "sha256": "aa"}],
    }}
    comp = next(c for c in grade_env(base)["components"]
                if c["component"] == "post_install")
    assert comp["portable"] is False

    base["extras"]["post_install_inputs"].append(
        {"ref": "sha256:bb", "sha256": "bb"})
    comp = next(c for c in grade_env(base)["components"]
                if c["component"] == "post_install")
    assert comp["portable"] is True


def test_step_notes_shift_with_the_parent_steps():
    parent = EnvSpec.from_dict({
        "name": "p", "post_install": ["step-a", "step-b"],
        "step_notes": {"0": "parent note"}})
    child = EnvSpec.from_dict({
        "name": "c", "post_install": ["step-c"],
        "step_notes": {"0": "child note"}})
    merged = child.merged_onto(parent)
    assert merged.post_install == ["step-a", "step-b", "step-c"]
    # the child's note follows its step to index 2 — it must not clobber
    # the parent's note at index 0
    assert merged.step_notes == {"0": "parent note", "2": "child note"}


def test_diff_envs_does_not_collapse_platforms_or_kinds():
    old = {"platforms": {
        "linux-64": [{"kind": "conda", "name": "numpy", "version": "2.0"}],
        "osx-arm64": [{"kind": "conda", "name": "numpy", "version": "2.0"}]}}
    new = {"platforms": {
        "linux-64": [{"kind": "conda", "name": "numpy", "version": "2.0"}],
        "osx-arm64": [{"kind": "conda", "name": "numpy", "version": "2.1"}]}}
    d = diff_envs(old, new)
    assert d["changed"] == [{"name": "osx-arm64/conda:numpy",
                             "from": "2.0", "to": "2.1"}]

    # conda and pypi packages sharing a name do not mask each other
    old = {"platforms": {"linux-64": [
        {"kind": "conda", "name": "x", "version": "1"},
        {"kind": "pypi", "name": "x", "version": "9"}]}}
    new = {"platforms": {"linux-64": [
        {"kind": "conda", "name": "x", "version": "2"},
        {"kind": "pypi", "name": "x", "version": "9"}]}}
    assert diff_envs(old, new)["changed"] == [
        {"name": "linux-64/conda:x", "from": "1", "to": "2"}]


def test_pep503_and_layer_dep_names():
    assert _pep503("Typing_Extensions") == "typing-extensions"
    assert _layer_dep_name("glue ==1.7.0") == "glue"
    assert _layer_dep_name("tidyverse/glue@abc123") == "glue"
    assert _layer_dep_name("owner/Repo.jl@main") == "Repo"


def test_classify_delta_sees_source_identity_not_just_version():
    """A github build and the CRAN release of the same version are
    different artifacts — swapping one for the other is base drift."""
    gh = {"platforms": {}, "layers": {"cran": {"records": [
        {"name": "glue", "version": "1.7.0",
         "source": "github:tidyverse/glue@main", "remote_sha": "abc"}]}}}
    cran = {"platforms": {}, "layers": {"cran": {"records": [
        {"name": "glue", "version": "1.7.0",
         "source": "https://packagemanager.posit.co/cran/2026-07-01"}]}}}
    d = classify_delta(gh, cran)
    assert d["layerable"] is False
    assert "base drift" in d["why"]

    # but the same github artifact reached via @main vs @<sha> is IDENTICAL
    gh2 = {"platforms": {}, "layers": {"cran": {"records": [
        {"name": "glue", "version": "1.7.0",
         "source": "github:tidyverse/glue@abc", "remote_sha": "abc"}]}}}
    assert classify_delta(gh, gh2)["layerable"] is False   # nothing added
    assert "adds nothing" in classify_delta(gh, gh2)["why"]
