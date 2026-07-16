"""env_packages (weft-ui round 23 ask): the resolved package records,
wholesale, from the stored lock — one read, no solve, both canonical
shapes (per-platform conda/pypi records and layer records)."""

import pytest

from weft.api import Weft

CANONICAL = {
    "version": 2,
    "extras": {},
    "platforms": {
        "linux-64": [
            {"name": "python", "version": "3.12.1", "build": "h0abc_1",
             "kind": "conda", "sha256": "a" * 64},
            {"name": "emcee", "version": "3.1.6", "kind": "pypi",
             "sha256": "b" * 64},
        ],
        "osx-arm64": [
            {"name": "python", "version": "3.12.1", "build": "h9xyz_1",
             "kind": "conda", "sha256": "c" * 64},
        ],
    },
    "layers": {
        "cran": {"records": [
            {"name": "jsonlite", "version": "2.0.0", "deps": [],
             "sha256": "", "source": "https://cran.example/2026-07-01"},
        ]},
    },
}


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.store.put_env("env:v2:" + "e" * 64, "s" * 64, CANONICAL,
                    "lock: 1\n", "[workspace]\n",
                    ["linux-64", "osx-arm64"])
    return w


def test_lists_every_ecosystem_in_one_call(w):
    out = w.env_packages("env:v2:" + "e" * 64)
    assert out["count"] == 4
    by = {(p["name"], p["platform"]): p for p in out["packages"]}
    assert by[("python", "linux-64")]["ecosystem"] == "conda"
    assert by[("python", "linux-64")]["build"] == "h0abc_1"
    assert by[("python", "osx-arm64")]["version"] == "3.12.1"
    assert by[("emcee", "linux-64")]["ecosystem"] == "pypi"
    # layer records are source releases, not per-platform binaries
    assert by[("jsonlite", None)]["ecosystem"] == "cran"
    # sorted by name for stable rendering
    assert [p["name"] for p in out["packages"]] == sorted(
        p["name"] for p in out["packages"])


def test_platform_filter_keeps_layers_visible(w):
    out = w.env_packages("env:v2:" + "e" * 64, platform="osx-arm64")
    names = {(p["name"], p["platform"]) for p in out["packages"]}
    assert names == {("python", "osx-arm64"), ("jsonlite", None)}


def test_unknown_env_is_honest(w):
    out = w.env_packages("env:v1:" + "0" * 64)
    assert out["error"] == "task.invalid"
    assert "env_ensure" in str(out["hints"])
