"""Raw scheduler-directive escape hatch: validation (CLAUDE.md principle
#2 — every generated artifact gets an override; every override gets a
guardrail)."""

import pytest

from weft.adapters.slurm import validate_directives
from weft.errors import WeftError


def test_reasonable_directives_pass():
    ds = ["--constraint=ib", "--qos high", "-C haswell",
          "--ntasks-per-node=1", "--exclusive"]
    assert validate_directives(ds, "test") == ds


def test_managed_flags_refused_with_the_structured_lever_named():
    with pytest.raises(WeftError) as e:
        validate_directives(["--partition=gpu"], "test")
    assert e.value.hints["use_instead"] == "resources.partition"
    with pytest.raises(WeftError) as e:
        validate_directives(["--time=01:00:00"], "test")
    assert e.value.hints["use_instead"] == "resources.walltime"
    with pytest.raises(WeftError) as e:
        validate_directives(["-c 8"], "test")
    assert "resources.cpus" in e.value.hints["use_instead"]
    with pytest.raises(WeftError):
        validate_directives(["--ntasks=4"], "test")


def test_identity_and_env_flags_refused():
    for bad in ("--uid=0", "--gid 0", "--export=ALL"):
        with pytest.raises(WeftError) as e:
            validate_directives([bad], "test")
        assert e.value.code == "task.invalid"


def test_non_directives_refused():
    for bad in ("rm -rf /", "constraint=ib", ";--foo"):
        with pytest.raises(WeftError):
            validate_directives([bad], "test")


def test_empty_and_whitespace_skipped():
    assert validate_directives(["", "  ", "--nice=5"], "t") == ["--nice=5"]
