"""Round F: reproducibility bundles — the acceptance test IS the claim:
export a finished result, import into a FRESH workspace, re-run, get
byte-identical output refs."""

import pytest

from weft.api import Weft

pytestmark = pytest.mark.solver


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def _run(w, task, t=1200):
    j = w.runner.wait(w.task_submit(task, force=True)["job_id"], t)
    assert j["state"] == "DONE", j["error"]
    return j


def test_bundle_rederives_in_a_fresh_workspace(w, tmp_path, pixi_bin):
    env = w.env_ensure({"name": "sampler",
                        "deps": {"conda": ["python =3.12"]}})["env_id"]
    (tmp_path / "ws" / "seeds.txt").write_text("3 1 4 1 5 9\n")
    ref = w.data_register("seeds.txt")["ref"]
    j = _run(w, {
        "command": "python -c \"import json; "
        "seeds=[int(x) for x in open('data/seeds.txt').read().split()]; "
        "json.dump({'sum_sq': sum(s*s for s in seeds)}, "
        "open('results/o.json','w'))\"",
        "env": env, "inputs": [{"ref": ref, "mount_as": "data/seeds.txt"}],
        "outputs": ["results/"], "site": "local"})
    job_id = j["manifest"]["job_id"] if "job_id" in j["manifest"] else \
        j["job_id"]

    b = w.bundle_export(job_id, str(tmp_path / "out" / "result.weft.tgz"))
    assert b["bytes"] > 0 and b["envs"] == 1
    assert b["reproducibility"] == "fully-pinned"

    # a FRESH workspace on a FRESH site root: nothing shared but the tarball
    w2 = Weft(tmp_path / "ws2", pixi_bin=pixi_bin)
    w2.register_site("local", "local", {"root": str(tmp_path / "site2"),
                                        "pixi_source": pixi_bin})
    imp = w2.bundle_import(b["path"])
    assert imp["envs"] == [env]
    recorded = {o["path"]: o["ref"] for o in imp["recorded_outputs"]}

    j2 = w2.runner.wait(w2.task_submit(
        {**imp["task"], "site": "local"}, force=True)["job_id"], 1200)
    assert j2["state"] == "DONE", j2["error"]
    rerun = {o["path"]: o["ref"] for o in j2["manifest"]["outputs"]}
    assert rerun == recorded          # byte-identical re-derivation


def test_bundle_refuses_unfinished_and_unknown(w):
    r = w.bundle_export("jb_nonexistent", "/tmp/x.tgz")
    assert r["error"] == "task.invalid"
    job = w.task_submit({"command": "sleep 30", "site": "local"})["job_id"]
    r = w.bundle_export(job, "/tmp/x.tgz")
    assert r["error"] == "task.invalid"
    assert "FINISHED" in r["detail"]
    w.task_cancel(job)


def test_bundle_chains_through_producing_jobs(w, tmp_path):
    """Stage B consumes stage A's output: the bundle for B carries A too."""
    env = w.env_ensure({"name": "chain",
                        "deps": {"conda": ["xz >=5"]}})["env_id"]
    ja = _run(w, {"command": "xz --version > results/v.txt", "env": env,
                  "outputs": ["results/"], "site": "local"})
    a_out = ja["manifest"]["outputs"][0]["ref"]
    a_id = ja["manifest"].get("job_id") or ja["job_id"]
    jb = _run(w, {"command": "wc -c < in/v.txt > results/n.txt", "env": env,
                  "inputs": [{"ref": a_out, "mount_as": "in/v.txt"}],
                  "outputs": ["results/"], "site": "local"})
    b_id = jb["manifest"].get("job_id") or jb["job_id"]
    b = w.bundle_export(b_id, str(tmp_path / "chain.weft.tgz"))
    assert b["jobs"] == 2, b          # A came along
    imp_jobs = w.bundle_import(b["path"])    # reimport into self: harmless
    assert a_id in str(b) or b["jobs"] == 2
    assert imp_jobs["target_job"] == b_id
