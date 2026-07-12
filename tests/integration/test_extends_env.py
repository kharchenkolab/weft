"""L1: extends_env — freeze the parent's resolution, solve only the delta."""

import time

import pytest

from weft.api import Weft
from weft.overlay import classify_delta

pytestmark = pytest.mark.solver

BASE = {"name": "base", "deps": {"conda": ["python =3.12", "numpy", "scipy"]}}


@pytest.fixture
def w(tmp_path, pixi_bin):
    return Weft(tmp_path / "ws", pixi_bin=pixi_bin)


def _versions(w, env_id, kind="conda"):
    canon = w.store.get_env(env_id)["canonical"]
    return {p["name"]: (p["version"], p["build"])
            for plat in canon["platforms"].values()
            for p in plat if p["kind"] == kind}


def test_child_lock_is_a_superset_by_construction(w):
    parent = w.env_ensure(BASE)["env_id"]
    child = w.env_ensure({"name": "base+pypi", "extends_env": parent,
                          "deps": {"pypi": ["emcee"]}})
    assert "env_id" in child, child
    assert child["extends_env"] == parent

    pv, cv = _versions(w, parent), _versions(w, child["env_id"])
    # EVERY parent package is present at the SAME version+build: no base drift
    assert all(cv.get(n) == v for n, v in pv.items()), \
        {n: (v, cv.get(n)) for n, v in pv.items() if cv.get(n) != v}
    assert "emcee" in _versions(w, child["env_id"], "pypi")

    d = child["delta"]
    assert d["layerable"] is True
    assert d["pypi_added"] == ["emcee"] and not d["conda_added"]
    assert "overlay" in child["note"]


def test_conda_delta_is_honest_about_not_layering(w):
    parent = w.env_ensure(BASE)["env_id"]
    child = w.env_ensure({"name": "base+conda", "extends_env": parent,
                          "deps": {"conda": ["xz >=5"]}})
    assert "env_id" in child, child
    d = child["delta"]
    assert d["layerable"] is False
    assert "xz" in d["conda_added"]        # (plus whatever xz pulls in)
    assert "embedded prefixes" in d["why"]
    assert "deps.pypi" in d["why"]          # the fast-path workaround
    # ...but the base still did not drift
    pv, cv = _versions(w, parent), _versions(w, child["env_id"])
    assert all(cv.get(n) == v for n, v in pv.items())


def test_contradicting_delta_never_moves_the_base_silently(w):
    """extends_env freezes the base ON PURPOSE: a delta that contradicts it
    is a conflict, not a silent version change."""
    parent = w.env_ensure({"name": "pinned",
                           "deps": {"conda": ["python =3.11"]}})["env_id"]
    r = w.env_ensure({"name": "needs-newer", "extends_env": parent,
                      "deps": {"conda": ["python =3.13"]}})
    assert r["error"] == "env.layer_conflict", r
    assert r["hints"]["package"] == "python"
    assert r["hints"]["parent_version"].startswith("3.11")
    assert "`extends`" in r["hints"]["suggestion"]   # the free-solve lever

    # a REDUNDANT delta (already satisfied by the frozen base) is a no-op,
    # not a conflict
    ok = w.env_ensure({"name": "redundant", "extends_env": parent,
                       "deps": {"conda": ["python >=3.10"],
                                "pypi": ["emcee"]}})
    assert "env_id" in ok, ok
    assert ok["delta"]["layerable"] is True


def test_constrained_solve_is_faster_than_a_free_one(w):
    """The interactive win: 'add one package' collapses the search space."""
    parent = w.env_ensure(BASE)["env_id"]

    t0 = time.time()
    free = w.env_ensure({"name": "free", "deps": {
        "conda": ["python =3.12", "numpy", "scipy"], "pypi": ["emcee"]}},
        update=True)
    free_s = time.time() - t0

    t0 = time.time()
    layered = w.env_ensure({"name": "layered", "extends_env": parent,
                            "deps": {"pypi": ["emcee"]}}, update=True)
    layered_s = time.time() - t0

    assert "env_id" in free and "env_id" in layered
    # not a benchmark — just the property that pinning cannot be slower
    assert layered_s <= free_s + 1.0, (layered_s, free_s)


def test_classify_delta_rejects_base_drift():
    parent = {"platforms": {"linux-64": [
        {"kind": "conda", "name": "numpy", "version": "2.0.1", "build": "h1"}]}}
    drifted = {"platforms": {"linux-64": [
        {"kind": "conda", "name": "numpy", "version": "2.1.0", "build": "h1"}]}}
    d = classify_delta(parent, drifted)
    assert d["layerable"] is False
    assert "base drift" in d["why"]
