import pytest

from weft.errors import WeftError
from weft.policy import (allowed_partition, enforce_policy, site_policy,
                         storage_env_vars)

POLICY = {
    "partitions_allowed": ["standard", "short"],
    "max_gpus": 4,
    "max_concurrent_jobs": 10,
    "storage": {"large": "/groups/phys/me", "scratch": "/scratch/me",
                "node_tmp": "/tmp"},
    "notes": ["prefer nights for >1h jobs"],
}


def test_gpu_cap_names_the_rule_and_source():
    with pytest.raises(WeftError) as e:
        enforce_policy(POLICY, {"gpus": 8}, 0, "hpc")
    err = e.value
    assert err.code == "site.capability_violation"
    assert err.hints["rule"] == "max_gpus" and err.hints["limit"] == 4
    assert "user" in err.hints["source"]
    # within the cap: fine
    enforce_policy(POLICY, {"gpus": 4}, 0, "hpc")


def test_concurrency_cap():
    with pytest.raises(WeftError) as e:
        enforce_policy(POLICY, {}, 10, "hpc")
    assert e.value.hints["rule"] == "max_concurrent_jobs"
    enforce_policy(POLICY, {}, 9, "hpc")


def test_partition_allowlist():
    assert allowed_partition(POLICY, None, "hpc") == "standard"  # first allowed
    assert allowed_partition(POLICY, "short", "hpc") == "short"
    with pytest.raises(WeftError) as e:
        allowed_partition(POLICY, "gpu-greedy", "hpc")
    assert e.value.hints["allowed"] == ["standard", "short"]
    # no policy: configured value passes through
    assert allowed_partition({}, "anything", "hpc") == "anything"


def test_storage_roles_become_env_vars():
    env = storage_env_vars(POLICY)
    assert env == {
        "WEFT_STORAGE_LARGE": "/groups/phys/me",
        "WEFT_STORAGE_SCRATCH": "/scratch/me",
        "WEFT_STORAGE_NODE_TMP": "/tmp",
    }
    assert storage_env_vars({}) == {}


def test_site_policy_reader():
    assert site_policy({"config": {"policy": POLICY}}) == POLICY
    assert site_policy({"config": {}}) == {}
    assert site_policy(None) == {}
