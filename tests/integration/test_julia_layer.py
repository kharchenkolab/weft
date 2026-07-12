"""Julia layer: Manifest.toml-locked deps solved and realized."""

import pytest

from weft.api import Weft

pytestmark = [pytest.mark.solver, pytest.mark.slow]


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
