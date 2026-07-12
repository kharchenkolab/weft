"""Solver-core invariants: hash continuity, open deps map, layer checks."""

import pytest

from weft.errors import WeftError
from weft.ids import env_id
from weft.solvers import check_layer_requirements
from weft.spec import EnvSpec


def test_conda_only_specs_hash_exactly_as_before():
    """Cache continuity: the generalization must not orphan existing
    spec hashes or EnvIDs for conda/pypi-only specs."""
    d = {"name": "t", "deps": {"conda": ["python =3.12"], "pypi": ["zfit"]}}
    spec = EnvSpec.from_dict(d)
    td = spec.to_dict()
    assert td["deps"] == {"conda": ["python =3.12"], "pypi": ["zfit"]}
    assert spec.deps_extra == {}
    canonical = {"version": 1, "platforms": {"linux-64": []},
                 "extras": {"modules": [], "post_install": [],
                            "container_base": None, "env_vars": {}}}
    assert env_id(canonical).startswith("env:v1:")
    canonical["layers"] = {"cran": {"records": []}}
    assert env_id(canonical).startswith("env:v2:")


def test_extra_ecosystems_parse_and_merge():
    parent = EnvSpec.from_dict(
        {"name": "p", "deps": {"conda": ["r-base =4.4"],
                               "cran": ["data.table >=1.15"]}})
    child = EnvSpec.from_dict(
        {"deps": {"cran": ["data.table ==1.16.0", "lab/pkg@fix-branch"]}})
    merged = child.merged_onto(parent)
    assert merged.deps_extra["cran"] == ["data.table ==1.16.0",
                                         "lab/pkg@fix-branch"]
    assert "cran" in merged.to_dict()["deps"]
    # ordering of ecosystems is canonical in to_dict (hash stability)
    two = EnvSpec.from_dict({"deps": {"julia": ["DataFrames"],
                                      "cran": ["jsonlite"],
                                      "conda": ["r-base", "julia"]}})
    assert list(two.to_dict()["deps"]) == ["conda", "pypi", "cran", "julia"]


def test_layer_requirements_named_conflict():
    spec = EnvSpec.from_dict({"deps": {"conda": ["python =3.12"],
                                       "cran": ["jsonlite"]}})
    with pytest.raises(WeftError) as e:
        check_layer_requirements(spec, spec.deps_extra)
    err = e.value
    assert err.code == "env.layer_conflict"
    assert err.hints["needs"] == "r-base in deps.conda"
    assert "r-base" in err.hints["suggestion"]
    # satisfied: no raise
    ok = EnvSpec.from_dict({"deps": {"conda": ["r-base =4.4"],
                                     "cran": ["jsonlite"]}})
    check_layer_requirements(ok, ok.deps_extra)


def test_unknown_ecosystem_fails_fast(tmp_path, pixi_bin):
    from weft.api import Weft
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    r = w.env_ensure({"name": "typo", "deps": {"connda": ["x"]}})
    assert r["error"] == "task.invalid"
    assert "conda" in r["hints"]["registered"]
