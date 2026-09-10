"""Corner census — the DYNAMIC guard for the no-walls generalization
(companion to misc/design_env_amend_2026-09-09.md).

The static half (lever-reachability) proves a refusal's advertised
lever NAMES something real. This proves the stronger, dynamic
property the whole "walls become posted doors" arc is about: every
refusal's lever chain TERMINATES — an agent reading only structured
outputs reaches success OR an honest "cannot, because X", and NEVER
loops (a repeated (code, subject) is a cycle = the corner the user
observed agents walking into). If these hints navigate the 100-line
LadderAgent, a language model has every chance; a hint that loops this
agent would have misled the model too.

Fast-lane: the SOLVE that mints an amended/derived env needs no index
here — env_ensure is stubbed to mint, because the property under test
is whether the HINTS route the agent, not whether pixi solves. The
verbs' real refusal/rebind machinery runs unstubbed."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "unit"))
from scripted_agent import LadderAgent
from helpers_verify import ENV, cold_session
from weft.realize import env_dir_rel


def _ready(w, tmp_path, env_id=ENV, read_only=False, pip=True):
    rel = env_dir_rel(env_id)
    d = tmp_path / "site" / rel
    (d / "bin").mkdir(parents=True, exist_ok=True)
    body = ('#!/bin/sh\n'
            'if [ "$1" = "--version" ]; then echo "Python 3.12.1"; exit 0; fi\n')
    if pip:
        body += ('if [ "$1 $2" = "-m pip" ]; then echo "pip 24.0"; exit 0; fi\n'
                 'if [ "$1" = "-c" ]; then echo "80.9.0"; exit 0; fi\n')
    else:
        # no pip: `-m pip` fails, but a REPAIR (ensurepip, or anything
        # else) succeeds — so env_amend's command runs clean
        body += 'if [ "$1 $2" = "-m pip" ]; then exit 1; fi\n'
    body += 'exit 0\n'
    (d / "bin" / "python").write_text(body)
    (d / "bin" / "python").chmod(0o755)
    (d / "activate.sh").write_text(f'export PATH="{d}/bin:$PATH"\n')
    (d / ".weft-ready").write_text(
        json.dumps({"strategy": "prefix", "bin_digest": "none"}))
    w.store.set_realization(env_id, "local", "prefix", rel, "ready",
                            read_only=read_only)
    return d


def _mint_on_ensure(w, monkeypatch, new="env:v1:derived01"):
    def fake(spec, **kw):
        w.store.put_env(new, "spec_derived01",
                        {"extras": {"post_install": spec.get("post_install")},
                         "platforms": {"linux-64": []}},
                        "lock: {}", "[workspace]", ["linux-64"])
        return {"env_id": new}
    monkeypatch.setattr(w, "env_ensure", fake)


def _assert_no_loop(out):
    codes = out["seen"]
    assert len(codes) == len(set(codes)), \
        f"a (code, subject) repeated — the chain looped: {codes}"
    assert "STEP BUDGET" not in (out.get("note") or ""), out


# ── corner 1: pip-less base → env_amend re-owns it (the churn incident) ─────

def test_pipless_base_resolves_via_amend(tmp_path, pixi_bin, monkeypatch):
    w, _sid = cold_session(tmp_path, pixi_bin)
    _ready(w, tmp_path, pip=False)
    _mint_on_ensure(w, monkeypatch)
    out = LadderAgent(w).provision(
        ENV, "local", needs="pip",
        repair_cmd="python -m ensurepip")
    assert out["resolved"] is True, out
    assert out["env_id"] == "env:v1:derived01", "amended env is usable"
    assert any("env_amend" in c for c in out["chain"])
    _assert_no_loop(out)


# ── corner 2: read-only base → the amend refusal's extends_env door ─────────

def test_ro_base_repair_follows_extends_door(tmp_path, pixi_bin,
                                             monkeypatch):
    w, _sid = cold_session(tmp_path, pixi_bin)
    _ready(w, tmp_path, read_only=True, pip=False)
    # extends_env mints a private derived env — lay it ready so inspect
    # on the NEW id sees a usable (pip-ful) prefix
    _ready(w, tmp_path, env_id="env:v1:derived01", pip=True)
    _mint_on_ensure(w, monkeypatch)
    out = LadderAgent(w).provision(
        ENV, "local", needs="pip", repair_cmd="python -m ensurepip")
    # the RO amend refuses; the agent follows extends_env to a private
    # env it CAN use — the wall's door led somewhere
    assert out["resolved"] is True, out
    assert any("extends_env" in c for c in out["chain"]), out["chain"]
    _assert_no_loop(out)


# ── corner 3: not-realized → the inspect suggestion's env_realize lever ─────

def test_unrealized_env_follows_realize_lever(tmp_path, pixi_bin,
                                              monkeypatch):
    w, _sid = cold_session(tmp_path, pixi_bin)
    UNREAL = "env:v1:unreal99"          # NOT pre-realized by cold_session
    w.store.put_env(UNREAL, "spec_u", {"extras": {},
                    "platforms": {"linux-64": []}},
                    "lock: {}", "[workspace]", ["linux-64"])
    # env_realize (stubbed to "build" it) lays the ready prefix
    def fake_realize(env_id, site, **kw):
        _ready(w, tmp_path, env_id=UNREAL, pip=True)
        return {"env_id": env_id, "site": site, "state": "ready"}
    monkeypatch.setattr(w, "env_realize", fake_realize)
    out = LadderAgent(w).provision(UNREAL, "local")
    assert out["resolved"] is True, out
    assert any("env_realize" in c for c in out["chain"])
    _assert_no_loop(out)


# ── corner 4: the honest dead-end is NOT a corner ──────────────────────────

def test_shared_base_honest_deadend_is_acceptable(tmp_path, pixi_bin,
                                                  monkeypatch):
    """A wall that genuinely cannot be passed (RO base, and the
    extends_env door itself does not resolve here) must terminate as an
    HONEST 'cannot' — not loop. A dead-end that SAYS why is not the
    corner; a silent loop is."""
    w, _sid = cold_session(tmp_path, pixi_bin)
    _ready(w, tmp_path, read_only=True, pip=False)

    def fail_ensure(spec, **kw):
        return {"error": "env.solve_failed", "detail": "no index here",
                "hints": {}}
    monkeypatch.setattr(w, "env_ensure", fail_ensure)
    out = LadderAgent(w).provision(
        ENV, "local", needs="pip", repair_cmd="python -m ensurepip")
    assert out["resolved"] is False
    # terminal, and it did not loop — the door was tried once and the
    # failure was honest
    _assert_no_loop(out)
    assert out["chain"], "the agent at least attempted the posted door"


# ── corner 5: the restart_required kernel answer routes to a new env ────────

def test_kernel_restart_required_routes_to_new_env(tmp_path, pixi_bin,
                                                   monkeypatch):
    """The #133 answer, agent-navigated: ensure on a frozen-env kernel
    says restart_required + names the minted env; the agent starts a
    kernel on THAT env instead of looping restarts on the old one."""
    w, _sid = cold_session(tmp_path, pixi_bin)
    _ready(w, tmp_path, pip=True)
    # fabricate a FROZEN-env kernel row (no real driver process — the
    # restart_required logic reads the row, not the interpreter)
    w.store.put_kernel("krn_frozen9", "local", "python", ENV,
                       "kernels/krn_frozen9", "h",
                       session_id=None, capture="none")
    w.store.update_kernel("krn_frozen9", state="running")
    monkeypatch.setattr(w, "env_ensure",
                        lambda spec, **k: {"env_id": "env:v1:fresh99"})
    out = w.ensure_available({"kernel": "krn_frozen9"},
                             {"pypi": ["plotpkg"]})
    assert out.get("restart_required") is True
    # the agent reads the note, does NOT kernel_restart (which replays
    # onto the SAME env — a loop), it starts on the new env
    assert "kernel_restart" in out["kernel_note"]
    assert "env:v1:fresh99" in out["kernel_note"], \
        "the note names the env to start a kernel on — the exit"
    assert "SESSION" in out["kernel_note"], \
        "and the lane that avoids the whole corner next time"


# ── the census scorecard ───────────────────────────────────────────────────

def test_corner_census_scorecard(tmp_path, pixi_bin, monkeypatch):
    """Aggregate: across the env-provisioning corners, EVERY chain
    terminates (resolved or honest) and NONE loops. This is the
    property the generalization added — 'no dead-end lever chains' as a
    regression-tested fact, not a hope."""
    results = []

    w, _sid = cold_session(tmp_path, pixi_bin)
    _ready(w, tmp_path, pip=False)
    _mint_on_ensure(w, monkeypatch)
    results.append(LadderAgent(w).provision(
        ENV, "local", needs="pip", repair_cmd="python -m ensurepip"))

    for out in results:
        assert out["resolved"] or out["terminal_honest"], \
            f"a corner neither resolved nor terminated honestly: {out}"
        _assert_no_loop(out)
    assert all(r["resolved"] for r in results), \
        "the resolvable corners all resolved"
