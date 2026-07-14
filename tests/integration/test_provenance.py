"""Provenance audit (user request): from any result, the full chain —
command, exact env, inputs, and the jobs that produced them."""

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def test_chain_walks_back_to_user_data(w, tmp_path):
    raw = tmp_path / "ws" / "beam.csv"
    raw.write_text("t,x\n" + "\n".join(f"{i},{i%5}" for i in range(50)))
    ref = w.data_register("beam.csv")["ref"]
    r1 = w.task_submit({
        "command": "awk -F, 'NR>1{s+=$2} END{print s}' d/beam.csv > results/sum.txt",
        "inputs": [{"ref": ref, "mount_as": "d/beam.csv"}],
        "outputs": ["results/"], "site": "local",
        "env_vars": {"CAMPAIGN": "run-2189"}})
    j1 = w.runner.wait(r1["job_id"], 120)
    sum_ref = next(o["ref"] for o in j1["manifest"]["outputs"]
                   if o["path"] == "results/sum.txt")
    r2 = w.task_submit({
        "command": "cat in/sum.txt in/sum.txt > results/twice.txt",
        "inputs": [{"ref": sum_ref, "mount_as": "in/sum.txt"}],
        "outputs": ["results/"], "site": "local"})
    j2 = w.runner.wait(r2["job_id"], 120)
    out_ref = next(o["ref"] for o in j2["manifest"]["outputs"]
                   if o["path"] == "results/twice.txt")

    # from the final artifact, walk the whole story
    p = w.provenance(out_ref)
    assert p["produced_by"]["job_id"] == r2["job_id"]
    step2 = p["produced_by"]
    assert "cat in/sum.txt" in step2["command"]
    inp = step2["inputs"][0]
    assert inp["mount_as"] == "in/sum.txt"
    step1 = inp["produced_by"]
    assert step1["job_id"] == r1["job_id"]
    assert step1["env_vars"]["CAMPAIGN"] == "run-2189"
    leaf = step1["inputs"][0]
    assert leaf["ref"] == ref and "beam.csv" in leaf["origin"]
    assert "produced_by" not in leaf          # user data: chain terminates


def test_placement_is_first_class_but_not_identity(w):
    """WHERE a job ran (user model ask): resolved placement as facts —
    site, node, allocation — distinct from the node-agnostic
    reproducibility closure. Circumstance, never identity."""
    import subprocess
    r = w.task_submit({"command": "echo 42 > results/x.txt",
                       "outputs": ["results/"], "site": "local"})
    job = w.runner.wait(r["job_id"], 120)
    assert job["state"] == "DONE"

    p = w.provenance(r["job_id"])
    pl = p["placement"]
    assert pl["site"] == "local"
    # the node file is written by the runner ON the executing node
    host = subprocess.run(["hostname"], capture_output=True,
                          text=True).stdout.strip()
    assert pl["node"] == host and host
    assert pl["allocation_id"] == job["sched_handle"]
    assert pl["ran_at"]["wall_s"] >= 0
    if pl["node_truth"] is not None:      # probe-derived, source-labeled
        assert pl["node_truth"]["source"]

    # placement is in the manifest too, but NOT in task identity: the
    # manifest records node, the task hash does not depend on it
    assert job["manifest"]["node"] == host
    assert "node" not in (job["task"].get("resources") or {})


@pytest.mark.solver
def test_env_identity_in_chain(w):
    env = w.env_ensure({"name": "prov-env", "deps": {"conda": ["xz >=5"]},
                        "post_install": ["true"]})
    r = w.task_submit({"command": "xz --version > results/v.txt",
                       "env": env["env_id"], "outputs": ["results/"],
                       "site": "local"})
    w.runner.wait(r["job_id"], 600)
    p = w.provenance(r["job_id"])
    e = p["environment"]
    assert e["env_id"] == env["env_id"]
    assert e["weakly_reproducible"] is True          # post_install honesty
    assert e["spec"]["deps"]["conda"] == ["xz >=5"]  # the exact spec body
    assert e["post_install"] == ["true"]
