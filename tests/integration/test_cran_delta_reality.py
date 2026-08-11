"""Reality: the aba 1.2 measured shape, in miniature, against real
indexes (solver lane). Their trace: conda layer binary-installed 164
r-* packages in ~1 min, then the cran layer spent 11.7 min re-building
25 conda-satisfied closure members from source around ONE github
package. With the solve-time delta the layer must contain exactly the
github package, and realization must be seconds, not minutes."""

import time

import pytest

from weft.api import Weft
from weft.spec import current_platform

pytestmark = pytest.mark.solver

# a github ref whose Imports (cli, glue, rlang) the conda layer covers —
# the closure collapses to nothing and only the ref itself installs
GH_REF = "r-lib/lifecycle"
CONDA = ["r-base", "r-cli", "r-glue", "r-rlang"]


def test_conda_satisfied_closure_realizes_in_seconds(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    out = w.env_ensure({"name": "cran-delta-reality",
                        "platforms": [current_platform()],
                        "deps": {"conda": CONDA, "cran": [GH_REF]}})
    assert "env_id" in out, out
    env_id = out["env_id"]

    row = w.store.get_env(env_id)
    layer = row["canonical"]["layers"]["cran"]
    names = [r["name"] for r in layer["records"]]
    assert names == ["lifecycle"], \
        f"the delta must leave ONLY the github package: {names}"
    satisfied = {s["name"] for s in layer.get("satisfied_by_conda", [])}
    # lifecycle's Imports at HEAD (cli, rlang at minimum — upstream may
    # trim); everything it does import must be conda-satisfied, none in
    # the layer
    assert {"cli", "rlang"} <= satisfied, satisfied
    assert "__linux__" not in layer["snapshot"], \
        "locks are platform-neutral now"

    t0 = time.time()
    r = w.env_realize(env_id, "local")
    dt = time.time() - t0
    assert not (isinstance(r, dict) and r.get("error")), r
    events = [e["kind"] for e in w.store.events_since(0, 2000)]
    assert "realize.prefix" in events, "the conda build narrates now"
    assert "realize.layer" in events
    # COST budget: one pure-R github install. The pre-delta shape was
    # >10 min here (25 source builds); allow generous slack for the
    # conda prefix install + the single tarball build.
    assert dt < 420, f"realize took {dt:.0f}s — the delta didn't hold"

    # the cran LAYER specifically must be fast: its events bracket it
    evs = w.store.events_since(0, 2000)
    lay = [e for e in evs if e["kind"] == "realize.layer.done"
           and e.get("layer") == "cran"]
    assert lay and lay[0]["elapsed_s"] < 180, lay
    print(f"[reality] realize {dt:.1f}s; cran layer "
          f"{lay[0]['elapsed_s']:.1f}s; satisfied_by_conda={sorted(satisfied)}")
