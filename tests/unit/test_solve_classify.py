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


CONFLICT_STDERR = """\
  × failed to solve the conda requirements of 'default' 'linux-64'
  ╰─▶ Cannot solve the request because of: dep ==1.6 conflicts with
      the pinned dep ==1.9
"""


def test_failing_solve_persists_stderr_next_to_manifest(monkeypatch,
                                                        tmp_path):
    """Forensics survive a swallowed exception (aba incident: the
    caller ate the raise; the solve dir kept pixi.toml and NOTHING
    else). The full stderr now lives next to the manifest."""
    monkeypatch.setattr(lockmod.subprocess, "run",
                        _fake_run(CONFLICT_STDERR))
    with pytest.raises(WeftError) as e:
        lockmod.solve(_spec(), tmp_path / "w")
    assert e.value.code == "env.solve_conflict"
    err_file = tmp_path / "w" / "solve.err"
    assert err_file.exists()
    assert "conflicts with" in err_file.read_text()


def test_failing_solve_emits_an_event(tmp_path, pixi_bin, monkeypatch):
    """The failure exists OUTSIDE the exception: an event a UI renders
    even when the caller swallows the raise."""
    from weft import envman as envmod
    from weft.api import Weft

    def dead_solve(spec, workdir, pixi_bin="pixi"):
        raise WeftError("env.solve_conflict",
                        f"spec '{spec.name}' is unsatisfiable as pinned",
                        stage="solve",
                        hints={"solver_message": "dep ==1.6 vs ==1.9"})

    monkeypatch.setattr(envmod, "solve", dead_solve)
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    out = w.env_ensure({"name": "clash", "deps": {"conda": ["python"]},
                        "platforms": ["linux-64"]})
    assert out["error"] == "env.solve_conflict"
    evs = [e for e in w.store.events_since(0, 100)
           if e["kind"] == "env.solve_conflict"]
    assert evs, "solve failure must emit an event"
    assert "1.6" in evs[0]["tail"]
    assert evs[0]["solve_dir"] and evs[0]["code"] == "env.solve_conflict"
