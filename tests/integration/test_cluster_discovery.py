"""Round A: alien-cluster orientation — GRES, partition metadata,
associations/QOS ceilings, per-partition ETAs, module enumeration,
storage candidates. Everything an agent needs BEFORE its first submit."""

import pytest

from weft.api import Weft

pytestmark = pytest.mark.docker


@pytest.fixture
def w(tmp_path, pixi_bin, slurm_site):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("hpc", "slurm", {
        "host": slurm_site["host"], "port": slurm_site["port"],
        "user": slurm_site["user"], "ssh_opts": slurm_site["ssh_opts"],
        "root": slurm_site["root"], "pixi_source": pixi_bin,
        "modules_init": "export MODULEPATH=/opt/site-modules",
    })
    w.runner.poll_interval = 0.4
    return w


def test_partition_records_carry_gres_and_features(w):
    caps = w.sites_describe("hpc")["capabilities"]
    assert caps["schema"] == "capabilities:v2"
    parts = {p["name"]: p for p in caps["scheduler"]["partitions"]}
    assert {"standard", "short", "gpu"} <= set(parts)
    gpu = parts["gpu"]
    assert gpu["gres"] == [{"type": "gpu", "model": "fake", "count": 2}]
    assert "avx512" in gpu["features"]
    # scontrol detail merged in
    assert gpu["max_walltime"] == "2:00:00"
    assert "priority_tier" in gpu or "oversubscribe" in gpu


def test_associations_and_qos_ceilings(w):
    a = w.site_associations("hpc")
    assert a["associations"], a
    assoc = a["associations"][0]
    assert assoc["account"] == "phys"
    assert "normal" in assoc["allowed_qos"]
    qos = {q["name"]: q for q in a["qos"]}
    assert qos["gpuq"]["limits_per_user"]["gpu"] == 4
    assert qos["normal"]["limits_per_user"]["cpu"] == 32
    assert qos["long"]["max_wall"] == "7-00:00:00"
    assert a["fairshare"]["factor"] == pytest.approx(0.4321)


def test_live_load_has_gpu_occupancy(w):
    load = w.site_load("hpc", fresh=True)
    gpu = load["partitions"]["gpu"]
    assert gpu["gpus_total"] == 2
    assert gpu["gpus_idle"] + gpu["gpus_allocated"] == 2
    assert load["my_associations"], "fake accounting should be visible"


def test_eta_comparison_across_partitions(w):
    out = w.site_load("hpc", resources={"cpus": 1, "walltime": "00:05:00"},
                      fresh=True, partitions=["standard", "short"])
    est = out["start_estimates"]
    assert set(est) == {"standard", "short"}
    for e in est.values():
        assert "estimated_start" in e


def test_module_enumeration(w):
    inv = w.module_list("hpc")
    assert inv["module_system"] == "ok"
    assert any("espresso" in m for m in inv["modules"]), inv
    hit = w.module_list("hpc", search="espresso")
    assert hit["total"] >= 1
    miss = w.module_list("hpc", search="no-such-thing")
    assert miss["total"] == 0


def test_storage_candidates_probed(w):
    caps = w.sites_describe("hpc")["capabilities"]
    cands = {c["path"]: c for c in caps["storage"]["candidates"]}
    assert "/tmp" in cands
    assert cands["/tmp"]["writable"] is True
    assert any(c["path"].startswith("/home") for c in cands.values())


def test_gpu_ask_routes_to_the_gpu_partition(w):
    """The submit plan should refuse a 4-GPU ask (max 2) with the honest
    ceiling, and accept a 2-GPU ask naming the fitting partition."""
    r = w.task_submit({"command": "true", "resources": {"gpus": 4},
                       "site": "hpc"}, dry_run=True)
    assert r.get("error") == "site.capability_violation", r
    assert r["hints"]["gpus"]["max"] == 2

    r = w.task_submit({"command": "true", "resources": {"gpus": 2},
                       "site": "hpc"}, dry_run=True)
    assert "error" not in r, r
