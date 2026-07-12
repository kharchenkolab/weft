"""Round A: slurm discovery parsers, exercised on captured real-cluster
output shapes (the fixture validates the live paths; these pin the corner
cases a small fixture can't produce)."""

from weft.adapters.slurm import parse_gres, parse_tres
from weft.capability import normalize_probe, satisfies_resources


def test_parse_gres_shapes():
    assert parse_gres("gpu:a100:4(S:0-1)") == [
        {"type": "gpu", "model": "a100", "count": 4}]
    assert parse_gres("gpu:4") == [{"type": "gpu", "model": None, "count": 4}]
    assert parse_gres("gpu:fake:2,shard:8") == [
        {"type": "gpu", "model": "fake", "count": 2},
        {"type": "shard", "model": None, "count": 8}]
    assert parse_gres("(null)") == []
    assert parse_gres("") == []
    # per-node usage suffix as GresUsed prints it
    assert parse_gres("gpu:fake:1(IDX:0)") == [
        {"type": "gpu", "model": "fake", "count": 1}]


def test_parse_tres_shapes():
    assert parse_tres("cpu=32,gres/gpu=2,mem=64G") == {
        "cpu": 32, "gpu": 2, "mem": "64G"}
    assert parse_tres("") == {}
    assert parse_tres("node=4") == {"node": 4}


def test_gpu_ask_validates_against_partition_gres():
    """Login nodes have no GPUs; the partitions do. A 2-GPU ask must pass
    where partition GRES covers it and fail with the honest max where not."""
    caps = {"cpus": 8, "mem_gb": 4, "gpus": []}   # login-node view
    parts = [
        {"name": "standard", "cpus_per_node": 8, "mem_gb_per_node": 4,
         "max_walltime": "01:00:00", "gres": []},
        {"name": "gpu", "cpus_per_node": 8, "mem_gb_per_node": 4,
         "max_walltime": "02:00:00",
         "gres": [{"type": "gpu", "model": "fake", "count": 2}]},
    ]
    ok, hints = satisfies_resources(caps, {"cpus": 1, "gpus": 2}, parts)
    assert ok, hints
    assert hints["fitting_partitions"] == ["gpu"]   # not standard

    ok, hints = satisfies_resources(caps, {"cpus": 1, "gpus": 4}, parts)
    assert not ok
    assert hints["gpus"]["max"] == 2


def test_capabilities_v2_markers():
    caps = normalize_probe({"hostname": "login01", "cpus": 8})
    assert caps["schema"] == "capabilities:v2"
    assert caps["measured_on"] == "login01"
    assert caps["probed_at"] > 0
