"""A3 kernel promotion (transcript manifests) and B2 offline CRAN layers."""

import pytest

from weft.api import Weft

SNAP = {"cran_snapshot": "2026-07-01"}


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.3
    return w


def test_kernel_promote_makes_a_transcript_manifest(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    w.kernel_exec(k, "grid = list(range(100))")
    w.kernel_exec(k, "chi2 = sum(v*v for v in grid)")
    r = w.kernel_exec(
        k, "import os\n"
           "open(os.environ['WEFT_BLOCK_DIR'] + '/chi2.txt', 'w')"
           ".write(str(chi2))")
    assert r["rc"] == 0

    m = w.kernel_promote(k, blocks=[r["block"]])
    assert m["schema"] == "manifest:v1"
    assert m["reproducibility"] == "transcript"      # the honest rung
    # the FULL chain that produced the state is recorded, not just the block
    assert [b["block"] for b in m["transcript"]] == [0, 1, 2]
    assert all(b["rc"] == 0 for b in m["transcript"])
    art = next(o for o in m["outputs"] if o["path"].endswith("chi2.txt"))
    assert art["preview"]["lines"] == ["328350"]

    # it's a first-class record: provenance and task_result see it
    p = w.provenance(m["job_id"])
    assert p["reproducibility"] == "transcript"
    assert w.task_result(m["job_id"])["job_id"] == m["job_id"]

    # failed blocks may not be promoted
    bad = w.kernel_exec(k, "raise ValueError('nope')")
    err = w.kernel_promote(k, blocks=[bad["block"]])
    assert err["error"] == "task.invalid" and "successful" in err["detail"]
    w.kernel_stop(k)


@pytest.mark.solver
@pytest.mark.slow
def test_cran_layer_packed_for_airgapped_site(tmp_path, pixi_bin):
    """No index access from the site: the cran layer is downloaded on the
    controller, shipped as a CAS blob, and installed offline in dependency
    order (design B2)."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("dark", "local", {
        "root": str(tmp_path / "site-dark"), "pixi_source": pixi_bin,
        "capabilities_override": {"internet": False,
                                  "runtimes": {"apptainer": "", "docker": False}},
    })
    env = w.env_ensure({
        "name": "r-dark",
        # the toolchain must come from the conda layer: offline R packages
        # build from source on the site
        "deps": {"conda": ["r-base =4.4", "c-compiler", "make"],
                 "cran": ["jsonlite"]},
        "system_requirements": SNAP})
    assert "env_id" in env, env
    r = w.task_submit({
        "command": "Rscript -e 'cat(jsonlite::toJSON(1:2))' > results/j.txt",
        "env": env["env_id"], "outputs": ["results/"], "site": "dark"})
    job = w.runner.wait(r["job_id"], 3600)
    assert job["state"] == "DONE", job["error"]
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/j.txt")
    assert out["preview"]["lines"] == ["[1,2]"]
    real = w.store.get_realization(env["env_id"], "dark")
    assert real["strategy"] == "packed"
    ev = [e for e in w.events_poll(0, 800, compact=False)["events"]
          if e["kind"] == "realize.layer"]
    assert any(e["layer"] == "cran" and e["offline"] for e in ev)


@pytest.mark.solver
def test_unpackable_layer_says_so(tmp_path, pixi_bin):
    """Julia has no packer yet: an air-gapped site must fail with a cause,
    not a mystery."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("dark", "local", {
        "root": str(tmp_path / "site-dark2"), "pixi_source": pixi_bin,
        "capabilities_override": {"internet": False,
                                  "runtimes": {"apptainer": "", "docker": False}},
    })
    env = w.env_ensure({"name": "jl-dark",
                        "deps": {"conda": ["julia"], "julia": ["Example"]}})
    assert "env_id" in env, env
    r = w.task_submit({"command": "true", "env": env["env_id"], "site": "dark"})
    job = w.runner.wait(r["job_id"], 1800)
    assert job["state"] == "FAILED"
    assert job["error"]["error"] == "env.unsatisfiable_on_site"
    assert job["error"]["hints"]["layer"] == "julia"
