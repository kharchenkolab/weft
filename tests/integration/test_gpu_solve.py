"""Lock-only CUDA stack solve: proves a GPU env is solvable from a GPU-less
controller (system-requirements assert the target's driver). No packages
are downloaded — `pixi lock` touches repodata only."""

import pytest

from weft.lock import solve
from weft.spec import EnvSpec

pytestmark = [pytest.mark.solver, pytest.mark.slow]


def test_pytorch_cuda_lock_resolves_gpu_builds(tmp_path, pixi_bin):
    spec = EnvSpec.from_dict({
        "name": "torch-gpu",
        "platforms": ["linux-64"],
        "deps": {"conda": ["python =3.12", "pytorch 2.* *cuda*"]},
        "variants": {"linux-64": {"conda": ["cuda-version <=12.6"]}},
        "system_requirements": {"cuda": "12.6"},
    })
    r = solve(spec, tmp_path, pixi_bin)
    pkgs = {p["name"]: p for p in r.canonical["platforms"]["linux-64"]}
    assert "pytorch" in pkgs
    # the solver must have chosen a CUDA build, not the CPU fallback
    assert "cuda" in pkgs["pytorch"]["build"], pkgs["pytorch"]
    assert "cuda-version" in pkgs
    assert pkgs["cuda-version"]["version"].startswith("12.6")
