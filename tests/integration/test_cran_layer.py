"""First-class CRAN/GitHub R deps: dated-snapshot solver (design-next §2)."""

import pytest

from weft.api import Weft

pytestmark = [pytest.mark.solver, pytest.mark.slow]

BASE = {"conda": ["r-base =4.4"]}


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


SNAP = {"cran_snapshot": "2026-07-01"}  # determinism across test days


def test_cran_solve_lock_and_identity(w):
    spec = {"name": "r-cran", "deps": {**BASE, "cran": ["jsonlite"]},
            "system_requirements": SNAP}
    d = w.env_ensure(spec, dry_run=True)
    assert "layers" in d, d
    assert d["layers"]["cran"]["packages"] >= 1
    assert d["env_id"].startswith("env:v2:")
    d2 = w.env_ensure(spec, dry_run=True)
    assert d2["env_id"] == d["env_id"]  # deterministic under pinned snapshot


def test_github_ref_pins_commit_sha(w):
    env = w.env_ensure({"name": "r-gh",
                        "deps": {**BASE, "cran": ["tidyverse/glue@main"]},
                        "system_requirements": SNAP})
    assert "env_id" in env, env
    rec = w.env_why(env["env_id"], "glue")
    assert rec["ecosystem"] == "cran"
    assert len(rec["record"]["remote_sha"]) == 40  # branch → exact commit
    assert rec["record"]["tarball"].endswith(rec["record"]["remote_sha"])


def test_wrong_exact_pin_names_the_fix(w):
    r = w.env_ensure({"name": "bad-pin",
                      "deps": {**BASE, "cran": ["jsonlite ==0.0.1"]},
                      "system_requirements": SNAP})
    assert r["error"] == "env.solve_conflict"
    assert "change it to ==" in r["hints"]["suggestion"]
    assert r["hints"]["snapshot"].endswith("2026-07-01")


def test_layer_conflict_without_r_base(w):
    r = w.env_ensure({"name": "no-r", "deps": {"conda": ["python =3.12"],
                                               "cran": ["jsonlite"]}})
    assert r["error"] == "env.layer_conflict"
    assert r["hints"]["needs"] == "r-base in deps.conda"


def test_cran_layer_realizes_and_runs(w):
    env = w.env_ensure({"name": "r-run",
                        "deps": {**BASE, "cran": ["jsonlite"]},
                        "system_requirements": SNAP})
    assert "env_id" in env, env
    r = w.task_submit({
        "command": "Rscript -e 'cat(jsonlite::toJSON(list(ok=TRUE)))' "
                   "> results/out.txt",
        "env": env["env_id"], "outputs": ["results/"], "site": "local",
    })
    job = w.runner.wait(r["job_id"], 1800)
    assert job["state"] == "DONE", job["error"]
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/out.txt")
    assert out["preview"]["lines"] == ['{"ok":[true]}']
    kinds = [e["kind"] for e in w.events_poll(0, 800, compact=False)["events"]]
    assert "realize.layer" in kinds and "realize.layer.done" in kinds
