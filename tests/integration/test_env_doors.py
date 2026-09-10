"""No-walls round (aba2 capable-agent contract, Legs 1-2 read-only
door): where weft MEDIATES interactive repair it must not be
capability-subtracting relative to raw ssh — an agent should SEE what
it stands on and ACT inside the env without leaving the audited
surface, and every wall it does keep must post its door.

env_inspect = ground truth of a realization (record facts + a live
interpreter probe: the pip/setuptools trap surfaced by name).
env_exec = the activated, audited equal of the site_exec an agent
already has — honest that changes are not re-owned, refusing on
shared/RO bases with the session/extends door named."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "unit"))
from helpers_verify import ENV, cold_session
from weft.adapters.base import ShimResult
from weft.errors import WeftError
from weft.realize import env_dir_rel


def _lay_ready_env(w, tmp_path, env_id=ENV, read_only=False):
    """A realized prefix on the local site with a real activate.sh so
    the live probe / exec actually run."""
    rel = env_dir_rel(env_id)
    d = tmp_path / "site" / rel
    (d / "bin").mkdir(parents=True, exist_ok=True)
    # a fake 'python' the probe/exec will find on PATH
    py = d / "bin" / "python"
    py.write_text("#!/bin/sh\n"
                  'if [ "$1" = "--version" ]; then echo "Python 3.12.1"; '
                  'exit 0; fi\n'
                  'if [ "$1 $2" = "-m pip" ]; then echo "pip 24.0 from x"; '
                  'exit 0; fi\n'
                  'if [ "$1" = "-c" ]; then echo "80.9.0"; exit 0; fi\n'
                  'exit 0\n')
    py.chmod(0o755)
    (d / "activate.sh").write_text(f'export PATH="{d}/bin:$PATH"\n')
    (d / ".weft-ready").write_text(json.dumps({"strategy": "prefix"}))
    w.store.set_realization(env_id, "local", "prefix", rel, "ready",
                            read_only=read_only)
    return d


# ── env_inspect: ground truth ──────────────────────────────────────────────

def test_inspect_reports_interpreter_facts(tmp_path, pixi_bin):
    w, _sid = cold_session(tmp_path, pixi_bin)
    _lay_ready_env(w, tmp_path)
    out = w.env_inspect(ENV, "local")
    assert out["realized"] is True
    assert out["strategy"] == "prefix" and out["read_only"] is False
    itp = out["interpreter"]
    assert itp["python_version"] == "Python 3.12.1"
    assert itp["pip_version"] == "24.0"
    assert itp["setuptools_version"] == "80.9.0"
    assert "package_count" in out and "grade" in out


def test_inspect_reports_missing_pip(tmp_path, pixi_bin):
    """A python with no pip reports pip_version None (the trap's first
    half; whether the NOTE fires also depends on site uv — pinned in
    the controlled-probe test below, since the test host has uv)."""
    w, _sid = cold_session(tmp_path, pixi_bin)
    d = _lay_ready_env(w, tmp_path)
    (d / "bin" / "python").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "Python 3.12.1"; exit 0; fi\n'
        'exit 1\n')
    (d / "bin" / "python").chmod(0o755)
    out = w.env_inspect(ENV, "local")
    assert out["interpreter"]["pip_version"] is None


def test_inspect_note_fires_when_pip_and_uv_both_absent(tmp_path,
                                                        pixi_bin,
                                                        monkeypatch):
    """The ONE fact the churn incident turned on — no pip AND no uv —
    surfaced by name. Probe output controlled so host uv can't mask it."""
    w, _sid = cold_session(tmp_path, pixi_bin)
    _lay_ready_env(w, tmp_path)
    ad = w.adapters["local"]
    monkeypatch.setattr(ad, "run_activated",
                        lambda *a, **k: ShimResult(
                            0, "python=/x/bin/python\n"
                               "python_version=Python 3.12.1\n"
                               "pip=\nsetuptools=\nuv=\n", ""))
    out = w.env_inspect(ENV, "local")
    assert out["interpreter"]["pip_version"] is None
    assert out["interpreter"]["has_uv"] is False
    assert "note" in out and "pip" in out["note"] and "uv" in out["note"]


def test_inspect_unrealized_names_the_lever(tmp_path, pixi_bin):
    w, _sid = cold_session(tmp_path, pixi_bin)
    w.store.put_env("env:v1:notreal01", "spec_notreal01",
                    {"extras": {}, "platforms": {"linux-64": []}},
                    "lock: {}", "[workspace]", ["linux-64"])
    out = w.env_inspect("env:v1:notreal01", "local")
    assert out["realized"] is False
    assert "env_realize" in out["suggestion"]
    assert "interpreter" not in out, "no probe when nothing is realized"


def test_inspect_is_read_only(tmp_path, pixi_bin):
    """No mutation, and last_used-style activity is not touched — pure
    observation."""
    w, _sid = cold_session(tmp_path, pixi_bin)
    _lay_ready_env(w, tmp_path)
    before = w.store.get_realization(ENV, "local")
    w.env_inspect(ENV, "local")
    after = w.store.get_realization(ENV, "local")
    assert before["state"] == after["state"] == "ready"


# ── env_exec: the activated diagnostic door ────────────────────────────────

def test_exec_runs_in_the_activated_env(tmp_path, pixi_bin):
    w, _sid = cold_session(tmp_path, pixi_bin)
    _lay_ready_env(w, tmp_path)
    out = w.env_exec(ENV, "local", 'echo "PATH=$PATH"',
                     why="which python am I on")
    assert out["rc"] == 0
    assert env_dir_rel(ENV).split("/")[-1] in out["stdout"], \
        "the activated env's bin is on PATH — activation happened"
    assert "re-owned" in out["note"], \
        "honest about non-re-ownership at the point of use"


def test_exec_audited_and_evented(tmp_path, pixi_bin):
    w, _sid = cold_session(tmp_path, pixi_bin)
    _lay_ready_env(w, tmp_path)
    w.env_exec(ENV, "local", "echo hi", why="probe")
    trail = w.audit_tail(10)["audit"]
    assert any(a["action"] == "env.exec" and a.get("why") == "probe"
               for a in trail), "audited like every diagnostic door"
    assert [e for e in w.store.events_since(0, 200)
            if e["kind"] == "env.exec"]


def test_exec_deny_list_applies(tmp_path, pixi_bin):
    w, _sid = cold_session(tmp_path, pixi_bin)
    _lay_ready_env(w, tmp_path)
    out = w.env_exec(ENV, "local", "shutdown -h now", why="oops")
    assert out["error"] == "task.invalid" or "deny" in str(out).lower()


def test_exec_refuses_read_only_base_naming_the_door(tmp_path, pixi_bin):
    w, _sid = cold_session(tmp_path, pixi_bin)
    _lay_ready_env(w, tmp_path, read_only=True)
    out = w.env_exec(ENV, "local", "pip install foo",
                     why="repair the base")
    assert out["error"] == "task.invalid"
    lv = out["hints"]["levers"]
    assert "extends_env" in lv["change"] and "session" in lv["change"], \
        "the wall posts its door: overlay or session, never in-place"


def test_exec_unrealized_refuses_with_realize_lever(tmp_path, pixi_bin):
    w, _sid = cold_session(tmp_path, pixi_bin)
    w.store.put_env("env:v1:notreal02", "spec_notreal02",
                    {"extras": {}, "platforms": {"linux-64": []}},
                    "lock: {}", "[workspace]", ["linux-64"])
    out = w.env_exec("env:v1:notreal02", "local", "echo x", why="x")
    assert out["error"] == "env.not_realized"
    assert "env_realize" in out["hints"]["suggestion"]


def test_exec_bounds_are_levers(tmp_path, pixi_bin):
    w, _sid = cold_session(tmp_path, pixi_bin)
    _lay_ready_env(w, tmp_path)
    out = w.env_exec(ENV, "local", "echo x", why="x", timeout=99999)
    assert out["error"] == "task.invalid", "out-of-range refuses loudly"


# ── the doors are public and contracted ────────────────────────────────────

def test_verbs_are_public_and_documented():
    from weft.api import PUBLIC_TOOLS, Weft
    for v in ("env_inspect", "env_exec"):
        assert v in PUBLIC_TOOLS
        assert (getattr(Weft, v).__doc__ or "").strip(), \
            f"{v} needs a docstring (public verb contract)"
