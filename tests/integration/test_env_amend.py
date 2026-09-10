"""env_amend (approved design, misc/design_env_amend_2026-09-09.md):
repair a realization IN PLACE and RE-OWN the result as a first-class
EnvID — the door that lets an agent fix an env without leaving weft
AND keeps identity honest by re-identifying immediately.

Invariants proven here: the amended id differs and carries the repair
as post_install; the site's prefix rebinds to it while the parent's
realization here goes missing; a frozen-env kernel re-points; a failed
repair leaves NO half-amended identity; shared/RO/non-prefix bases
refuse with the door named. The portability replay (amended env
realizes elsewhere as parent+repair) is a docker/reality proof, source-
pinned here."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "unit"))
from helpers_verify import ENV, cold_session
from weft.adapters.base import ShimResult
from weft.realize import env_dir_rel


def _lay(w, tmp_path, env_id=ENV, read_only=False, strategy="prefix"):
    rel = env_dir_rel(env_id)
    d = tmp_path / "site" / rel
    (d / "bin").mkdir(parents=True, exist_ok=True)
    (d / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
    (d / "bin" / "python").chmod(0o755)
    (d / "activate.sh").write_text(f'export PATH="{d}/bin:$PATH"\n')
    (d / ".weft-ready").write_text(
        json.dumps({"strategy": strategy, "bin_digest": "none"}))
    w.store.set_realization(env_id, "local", strategy, rel, "ready",
                            read_only=read_only)
    return d


def _fake_ensure(w, monkeypatch, amended="env:v1:amended001"):
    """env_ensure mints the amended env row (real solve needs an
    index); capture the spec it was handed."""
    seen = {}

    def fake(spec, **kw):
        seen["spec"] = spec
        w.store.put_env(amended, "spec_amended001",
                        {"extras": {"post_install": spec.get("post_install")},
                         "platforms": {"linux-64": []}},
                        "lock: {}", "[workspace]", ["linux-64"])
        return {"env_id": amended, "status": "solved"}

    monkeypatch.setattr(w, "env_ensure", fake)
    return seen


# ── the happy path: run, mint, rebind ──────────────────────────────────────

def test_amend_mints_and_rebinds(tmp_path, pixi_bin, monkeypatch):
    w, _sid = cold_session(tmp_path, pixi_bin)
    _lay(w, tmp_path)
    seen = _fake_ensure(w, monkeypatch)
    out = w.env_amend(ENV, "local",
                      "python -m pip install 'setuptools<81'",
                      why="pkg_resources trap")
    assert out["env_id"] == "env:v1:amended001"
    assert out["parent"] == ENV and out["rc"] == 0
    assert out["grade"] == "escape-hatch"
    # the repair is the amended env's post_install (spec algebra)
    assert seen["spec"]["extends_env"] == ENV
    assert seen["spec"]["post_install"] == [
        "python -m pip install 'setuptools<81'"]
    assert seen["spec"]["step_notes"] == {"0": "pkg_resources trap"}
    # rebind: amended env is realized HERE; parent released
    amreal = w.store.get_realization("env:v1:amended001", "local")
    assert amreal["state"] == "ready"
    assert amreal["location"] == env_dir_rel(ENV), \
        "the amended env's realization IS the mutated prefix"
    assert w.store.get_realization(ENV, "local")["state"] == "missing", \
        "the parent no longer realizes those bytes — honest"
    # the marker now certifies the amended env with a fresh digest
    d = tmp_path / "site" / env_dir_rel(ENV) / ".weft-ready"
    mk = json.loads(d.read_text())
    assert mk["amended_from"] == ENV and mk["bin_digest"] != "none"
    assert [e for e in w.store.events_since(0, 300)
            if e["kind"] == "env.amended"]


def test_amend_repoints_frozen_kernel(tmp_path, pixi_bin, monkeypatch):
    """A running kernel FROZEN on the parent env runs against the
    prefix that is now the amended bytes — its record must follow."""
    w, _sid = cold_session(tmp_path, pixi_bin)
    _lay(w, tmp_path)
    _fake_ensure(w, monkeypatch)
    # a running frozen-env kernel bound to this env
    w.store.put_kernel("krn_frozen01", "local", "python",
                       ENV, "kernels/krn_frozen01", "h",
                       session_id=None, capture="none")
    w.store.update_kernel("krn_frozen01", state="running")
    out = w.env_amend(ENV, "local", "echo fix", why="x")
    assert out["kernels_repointed"] == ["krn_frozen01"]
    assert w.store.get_kernel("krn_frozen01")["env_id"] == out["env_id"]
    assert [e for e in w.store.events_since(0, 300)
            if e["kind"] == "kernel.env_amended"]


# ── the invariant: memoization never crosses the divergence ────────────────

def test_parent_and_amended_are_distinct_identities(tmp_path, pixi_bin,
                                                    monkeypatch):
    w, _sid = cold_session(tmp_path, pixi_bin)
    _lay(w, tmp_path)
    _fake_ensure(w, monkeypatch)
    out = w.env_amend(ENV, "local", "echo x", why="x")
    assert out["env_id"] != ENV, "post_install is hashed → new EnvID"
    # a task citing the parent finds its realization MISSING here (must
    # rebuild pristine — never the amended bytes under the parent's name)
    assert w.store.get_realization(ENV, "local")["state"] == "missing"


# ── the atomic failure: no half-amended identity ───────────────────────────

def test_failed_repair_leaves_no_amended_id(tmp_path, pixi_bin,
                                            monkeypatch):
    w, _sid = cold_session(tmp_path, pixi_bin)
    d = _lay(w, tmp_path)
    minted = {"n": 0}

    def fake(spec, **kw):
        minted["n"] += 1
        return {"env_id": "env:v1:should_not"}

    monkeypatch.setattr(w, "env_ensure", fake)
    ad = w.adapters["local"]
    orig = ad.run_activated
    monkeypatch.setattr(ad, "run_activated",
                        lambda s, **k: ShimResult(1, "", "boom")
                        if "echo" in s else orig(s, **k))
    out = w.env_amend(ENV, "local", "echo boom", why="will fail")
    assert out["error"] == "env.realize_failed"
    assert minted["n"] == 0, "no identity minted on a failed repair"
    assert w.store.get_realization(ENV, "local")["state"] == "missing", \
        "the dirty prefix rebuilds pristine next use"


# ── the walls that stay walls, each posting its door ───────────────────────

def test_amend_refuses_read_only_base(tmp_path, pixi_bin):
    w, _sid = cold_session(tmp_path, pixi_bin)
    _lay(w, tmp_path, read_only=True)
    out = w.env_amend(ENV, "local", "pip install x", why="repair base")
    assert out["error"] == "task.invalid"
    lv = out["hints"]["levers"]
    assert "extends_env" in lv["extends_env"] and "session" in lv["session"]


def test_amend_refuses_squashfs_strategy(tmp_path, pixi_bin):
    w, _sid = cold_session(tmp_path, pixi_bin)
    _lay(w, tmp_path, strategy="squashfs")
    out = w.env_amend(ENV, "local", "echo x", why="x")
    assert out["error"] == "task.invalid"
    assert "immutable" in out["detail"] and "extends_env" in \
        out["hints"]["levers"]["extends_env"]


def test_amend_unrealized_refuses_with_lever(tmp_path, pixi_bin):
    w, _sid = cold_session(tmp_path, pixi_bin)
    w.store.put_env("env:v1:unreal01", "spec_unreal01",
                    {"extras": {}, "platforms": {"linux-64": []}},
                    "lock: {}", "[workspace]", ["linux-64"])
    out = w.env_amend("env:v1:unreal01", "local", "echo x", why="x")
    assert out["error"] == "env.not_realized"
    assert "env_realize" in out["hints"]["suggestion"]


def test_amend_deny_list(tmp_path, pixi_bin):
    w, _sid = cold_session(tmp_path, pixi_bin)
    _lay(w, tmp_path)
    out = w.env_amend(ENV, "local", "shutdown -h now", why="oops")
    assert out["error"] == "task.invalid"


def test_amend_audited(tmp_path, pixi_bin, monkeypatch):
    w, _sid = cold_session(tmp_path, pixi_bin)
    _lay(w, tmp_path)
    _fake_ensure(w, monkeypatch)
    w.env_amend(ENV, "local", "echo x", why="the reason")
    assert any(a["action"] == "env.amend" and a.get("why") == "the reason"
               for a in w.audit_tail(10)["audit"])


# ── public + documented ────────────────────────────────────────────────────

def test_amend_public_and_documented():
    from weft.api import PUBLIC_TOOLS, Weft
    assert "env_amend" in PUBLIC_TOOLS
    doc = (Weft.env_amend.__doc__ or "")
    assert "RE-OWN" in doc and "post_install" in doc, \
        "the contract (re-own via spec algebra) is in the docstring"
