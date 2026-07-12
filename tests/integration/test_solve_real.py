"""Real pixi solves — need network access to conda-forge (marker: solver)."""

import pytest

from weft.errors import WeftError
from weft.lock import solve
from weft.spec import EnvSpec

pytestmark = pytest.mark.solver


def test_tiny_solve_deterministic(tmp_path, pixi_bin):
    spec = EnvSpec.from_dict({"name": "tiny", "deps": {"conda": ["xz >=5"]}})
    r1 = solve(spec, tmp_path / "a", pixi_bin)
    r2 = solve(spec, tmp_path / "b", pixi_bin)
    assert r1.env_id == r2.env_id
    assert r1.env_id.startswith("env:v1:")
    names = [p["name"] for p in r1.canonical["platforms"]["linux-64"]]
    assert "xz" in names


def test_solve_conflict_is_structured(tmp_path, pixi_bin):
    spec = EnvSpec.from_dict(
        {"name": "broken", "deps": {"conda": ["python =3.12", "numpy ==1.19.5"]}}
    )
    with pytest.raises(WeftError) as e:
        solve(spec, tmp_path, pixi_bin)
    err = e.value
    assert err.code == "env.solve_conflict"
    assert err.stage == "solve"
    assert "user_pins" in err.hints and "solver_message" in err.hints
    d = err.to_dict()
    assert d["error"] == "env.solve_conflict" and d["hints"]["user_pins"]
