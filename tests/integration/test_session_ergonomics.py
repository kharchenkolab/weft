"""Eight-asks round C — session ergonomics. C1 (ask 4):
session_freezable answers "can this session still be frozen?" without
minting or realizing (the would-be snapshot spec, dry-run solved) —
callers used to discover unfreezable sessions inside a job submit's
error handler. C2 (ask 6): fast=False on the CRAN lane — the ask said
"confirm it works"; it did not EXIST (_materialize_rlib never consulted
fast; the same silent-no-op-lever class round #95 found on the pypi
overlay lane). C3 (ask 7): R adds carry the pypi lane's envelope —
solver_message on failures, shadows_base with R semantics (the rlib
precedes the base library in .libPaths(), so same-named packages MASK
the base's)."""

import pytest

from weft.api import Weft
from weft.errors import WeftError
from weft.session import SessionManager


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


# ------------------------------------------------------------- units

def test_validate_solve_carries_cran(w, monkeypatch):
    """C2's machinery: the pulled-forward check must ask the solver the
    SAME question the snapshot will — cran included."""
    asked = {}
    monkeypatch.setattr(
        w.sessions.envman, "ensure",
        lambda spec, **kw: asked.update(spec=spec) or {"env_id": "env:v1:x"})
    s = {"session_id": "ses_x", "base_env_id": "env:v1:base",
         "added_conda": [], "added_pypi": [], "added_cran": ["r-glue"]}
    monkeypatch.setattr(
        w.sessions.store, "get_env",
        lambda eid: {"spec_hash": "spec:v1:parent"})
    got = w.sessions._validate_solve(s, [], cran=["r-vctrs"])
    assert got == "env:v1:x"
    assert asked["spec"]["deps"]["cran"] == ["r-glue", "r-vctrs"]


def test_validate_solve_failure_names_cran_request(w, monkeypatch):
    def boom(spec, **kw):
        raise WeftError("env.solve_conflict", "no", stage="solve")
    monkeypatch.setattr(w.sessions.envman, "ensure", boom)
    monkeypatch.setattr(w.sessions.store, "get_env",
                        lambda eid: {"spec_hash": "spec:v1:p"})
    s = {"session_id": "s", "base_env_id": "e", "added_conda": [],
         "added_pypi": [], "added_cran": []}
    with pytest.raises(WeftError) as ei:
        w.sessions._validate_solve(s, [], cran=["r-broken"])
    assert ei.value.hints["requested"] == ["r-broken"]
    assert "nothing was installed" in ei.value.hints["note"]


def test_freezable_true_and_false_paths(w, monkeypatch):
    calls = {}

    def fake_get(sid):
        return {"session_id": sid, "base_env_id": "env:v1:b",
                "added_conda": [], "added_pypi": [],
                "installers": [{"cmd": "make install", "note": "tool"}]}
    monkeypatch.setattr(w.sessions, "_get", fake_get)
    monkeypatch.setattr(w.sessions.store, "get_env",
                        lambda eid: {"spec_hash": "spec:v1:p"})
    monkeypatch.setattr(
        w.sessions.envman, "ensure",
        lambda spec, dry_run=False, **kw: calls.update(dry=dry_run)
        or {"env_id": "env:v1:would"})
    out = w.session_freezable("ses_1")
    assert out["freezable"] is True
    assert out["would_be_env"] == "env:v1:would"
    assert calls["dry"] is True                    # never minted
    assert "escape-hatch" in out["grade_note"]     # installer taint
    assert "NOW" in out["note"]                    # honesty caveat

    def boom(spec, dry_run=False, **kw):
        raise WeftError("env.solve_conflict", "pin fight", stage="solve",
                        hints={"solver_message": "x vs y"})
    monkeypatch.setattr(w.sessions.envman, "ensure", boom)
    out = w.session_freezable("ses_1")
    assert out["freezable"] is False
    assert out["reason"]["error"] == "env.solve_conflict"


def test_rlib_shadows_semantics(w, tmp_path, monkeypatch):
    """R shadows_base: intersection of rlib and the base env's R
    library — probe failure is never a verdict."""
    base_lib = tmp_path / "site" / "envs" / "e1" / \
        ".pixi/envs/default/lib/R/library"
    rlib = tmp_path / "site" / "rlib"
    for d, pkgs in ((base_lib, ["Matrix", "jsonlite", "base"]),
                    (rlib, ["jsonlite", "mypkg"])):
        for p in pkgs:
            (d / p).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        w.sessions.store, "get_realization",
        lambda eid, site: {"location": "envs/e1", "strategy": "prefix"})
    s = {"base_env_id": "env:v1:b", "site": "local"}
    got = w.sessions._rlib_shadows(s, w.adapters["local"], str(rlib))
    assert got == ["jsonlite"]                     # masked, named
    # vanished base lib: honest empty, never an error
    monkeypatch.setattr(
        w.sessions.store, "get_realization",
        lambda eid, site: {"location": "envs/gone", "strategy": "prefix"})
    assert w.sessions._rlib_shadows(s, w.adapters["local"],
                                    str(rlib)) == []


def test_synth_spec_one_owner_for_probe_and_snapshot(w, monkeypatch):
    """The freezable probe and the snapshot must ask the same question
    (drift between them re-creates the surprise the verb removes)."""
    monkeypatch.setattr(w.sessions.store, "get_env",
                        lambda eid: {"spec_hash": "spec:v1:p"})
    s = {"session_id": "s1", "base_env_id": "e", "added_conda": ["zlib"],
         "added_pypi": ["idna"], "added_cran": ["r-glue"],
         "added_cran_repos": ["https://r.example"],
         "installers": [{"cmd": "./setup.sh"}]}
    spec = w.sessions._synth_spec(s)
    assert spec["deps"]["conda"] == ["zlib"]
    assert spec["deps"]["pypi"] == ["idna"]
    assert spec["deps"]["cran"] == ["r-glue"]
    assert spec["r_repositories"] == ["https://r.example"]
    assert spec["post_install"] == ["./setup.sh"]


# --------------------------------------------------- solver reality

@pytest.mark.solver
def test_freezable_on_a_real_session(w):
    env = w.env_ensure({"name": "frz", "deps": {"conda": ["python =3.12"]}})
    assert "error" not in env, env
    s = w.session_start(env["env_id"], "local")
    assert "error" not in s, s
    out = w.session_freezable(s["session_id"])
    assert out["freezable"] is True and out["solve_s"] >= 0
    assert "grade_note" not in out                 # no installers
    w.session_stop(s["session_id"])


@pytest.mark.solver
def test_cran_fast_false_solves_at_add(w):
    """C2 end-to-end: a cran add with fast=False validates via a real
    solve BEFORE installing; the result says so."""
    env = w.env_ensure({"name": "rfast",
                        "deps": {"conda": ["r-base =4.4", "r-jsonlite"]}})
    assert "error" not in env, env
    s = w.session_start(env["env_id"], "local")
    assert "error" not in s, s
    out = w.session_install(s["session_id"], cran=["glue"], fast=False)
    assert "error" not in out, out
    assert out.get("validated", "").startswith("env:v")     # solved at add
    w.session_stop(s["session_id"])
