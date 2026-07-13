"""Julia layer: Manifest.toml-locked deps solved and realized."""

import pytest

from weft.api import Weft
from weft.spec import current_platform

pytestmark = [pytest.mark.solver, pytest.mark.slow]

# conda-forge ships julia only for linux-64/osx-64: local-site julia is
# impossible on an Apple-silicon controller (the solve refuses honestly);
# linux lanes keep the coverage
needs_julia = pytest.mark.skipif(
    current_platform() == "osx-arm64",
    reason="no conda-forge julia for osx-arm64")


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def test_julia_requires_interpreter_layer(w):
    r = w.env_ensure({"name": "no-julia", "deps": {"julia": ["Example"]}})
    assert r["error"] == "env.layer_conflict"
    assert "julia" in r["hints"]["needs"]


@needs_julia
def test_julia_solve_and_run(w):
    env = w.env_ensure({"name": "jl", "deps": {"conda": ["julia"],
                                               "julia": ["Example"]}})
    assert "env_id" in env, env
    assert env["env_id"].startswith("env:v2:")
    rec = w.env_why(env["env_id"], "Example")
    assert rec["ecosystem"] == "julia"
    assert len(rec["record"]["tree_sha1"]) == 40   # content-addressed lock

    r = w.task_submit({
        "command": "julia -e 'using Example; println(hello(\"weft\"))' "
                   "> results/out.txt",
        "env": env["env_id"], "outputs": ["results/"], "site": "local"})
    job = w.runner.wait(r["job_id"], 2400)
    assert job["state"] == "DONE", job["error"]
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/out.txt")
    assert out["preview"]["lines"] == ["Hello, weft"]


@needs_julia
def test_julia_layer_packed_for_airgapped_site(tmp_path, pixi_bin):
    """G6 (design B2, julia): the controller instantiates the locked
    Manifest into a throwaway depot, ships the subset as one blob, and the
    site instantiates OFFLINE — the same seam the CRAN packer uses."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("dark", "local", {
        "root": str(tmp_path / "site-dark"), "pixi_source": pixi_bin,
        "capabilities_override": {"internet": False,
                                  "runtimes": {"apptainer": "",
                                               "docker": False}},
    })
    env = w.env_ensure({
        "name": "jl-dark",
        "deps": {"conda": ["julia =1.10"], "julia": ["Example ==0.5.5"]}})
    assert "env_id" in env, env
    r = w.task_submit({
        "command": "julia -e 'using Example; println(hello(\"dark\"))' "
                   "> results/o.txt",
        "env": env["env_id"], "outputs": ["results/"], "site": "dark"})
    job = w.runner.wait(r["job_id"], 3600)
    assert job["state"] == "DONE", job["error"]
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/o.txt")
    assert "dark" in out["preview"]["lines"][0]
    real = w.store.get_realization(env["env_id"], "dark")
    assert real["strategy"].endswith("packed")
