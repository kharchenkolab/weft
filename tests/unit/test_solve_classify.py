"""Solve-failure classification: a netfs-broken local cache is a
DETERMINISTIC failure and must not masquerade as index reachability
(it cost the reporter real debugging time chased as a network fault)."""

import subprocess

import pytest

from weft import lock as lockmod
from weft.errors import WeftError
from weft.spec import EnvSpec

# verbatim from cbe.next (RHEL10, pixi 0.72.2, /tmp on BeeGFS)
CBE_STDERR = """\
  × failed to map conda packages to their PyPI equivalents
  ├─▶ failed to fetch conda-pypi mapping from remote source
  ╰─▶ Cache error: File still doesn't exist
"""

NET_STDERR = "error: failed to fetch repodata: connection timed out"


def _spec():
    return EnvSpec.from_dict({"name": "t", "deps": {"conda": ["python"]},
                              "platforms": ["linux-64"]})


def _fake_run(stderr):
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout="",
                                           stderr=stderr)
    return run


def test_cache_error_is_deterministic_not_network(monkeypatch, tmp_path):
    monkeypatch.setattr(lockmod.subprocess, "run", _fake_run(CBE_STDERR))
    with pytest.raises(WeftError) as e:
        lockmod.solve(_spec(), tmp_path / "w")
    err = e.value
    assert err.code == "env.solve_failed"
    assert "cache" in err.detail.lower()
    assert err.retryable is False                  # deterministic
    assert "PIXI_CACHE_DIR" in err.hints["suggestion"]
    assert "cache_resolution" in err.hints         # what weft chose, why


def test_network_error_stays_retryable(monkeypatch, tmp_path):
    monkeypatch.setattr(lockmod.subprocess, "run", _fake_run(NET_STDERR))
    with pytest.raises(WeftError) as e:
        lockmod.solve(_spec(), tmp_path / "w")
    assert e.value.retryable is True
    assert "reach" in e.value.detail
