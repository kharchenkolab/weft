"""Live regressions for the three-reviewer audit: identity preservation
through revise, eviction liveness guards, GC recency, spec-note
persistence, adaptive-path cache hits."""

import time

import pytest

from weft.api import Weft

pytestmark = [pytest.mark.solver, pytest.mark.slow]

PY_BASE = {"name": "pybase", "deps": {"conda": ["python =3.12", "pip"]}}


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def _run(w, env_id, cmd="true", wait=1200):
    j = w.runner.wait(w.task_submit({"command": cmd, "env": env_id,
                                     "site": "local"}, force=True)["job_id"],
                      wait)
    assert j["state"] == "DONE", j["error"]
    return j


def test_revise_of_an_extends_env_child_keeps_the_base(w):
    """The gutted-env landmine: revising a child that extends a parent must
    re-pin the parent's whole set, not fresh-solve the delta alone."""
    parent = w.env_ensure(PY_BASE)["env_id"]
    child = w.env_ensure({"name": "kid", "extends_env": parent,
                          "deps": {"pypi": ["emcee"]}})["env_id"]
    r = w.env_revise(child, reason="test")
    # a fresh CONSTRAINED solve reproduces the identical env — the child
    # was restored, never re-minted with the parent amputated
    assert r["status"] == "restored", r


def test_eviction_refuses_while_a_job_runs_on_the_env(w):
    env = w.env_ensure({"name": "busy",
                        "deps": {"conda": ["xz >=5"]}})["env_id"]
    _run(w, env)     # realize first, so the sleep job starts fast
    job = w.task_submit({"command": "sleep 15", "env": env,
                         "site": "local"}, force=True)["job_id"]
    deadline = time.time() + 60
    while time.time() < deadline:
        if w.task_status(job)[0]["state"] == "RUNNING":
            break
        time.sleep(0.5)
    r = w.env_evict(env, "local")
    assert r["error"] == "env.evict_blocked"
    assert any(u["kind"] == "job" and u["id"] == job
               for u in r["hints"]["in_use"])
    assert w.runner.wait(job, 300)["state"] == "DONE"
    assert w.env_evict(env, "local")["state"] == "evicted"


def test_gc_recency_is_usage_not_state_change(w):
    env = w.env_ensure({"name": "hot", "deps": {"conda": ["xz >=5"]}})["env_id"]
    _run(w, env)
    # realized 30 days ago, by the store's account...
    w.store._write(
        "UPDATE realizations SET updated_at=? WHERE env_id=?",
        (time.time() - 30 * 86400, env))
    # ...but used again just now (any task touches recency)
    _run(w, env)
    p = w.gc_plan("local")["sites"]["local"]
    assert not p["evictable_realizations"]     # hot env is NOT a candidate

    # backdate the use too: now it is honestly idle, and the sweep evicts
    # it through evict() (with all its guards), not a raw rm
    w.store._write(
        "UPDATE realizations SET last_used=? WHERE env_id=?",
        (time.time() - 30 * 86400, env))
    p = w.gc_plan("local")["sites"]["local"]
    assert [r["env_id"] for r in p["evictable_realizations"]] == [env]
    done = w.gc_sweep("local", confirm=True)
    assert done["evicted_realizations"] == 1
    assert w.store.get_realization(env, "local")["state"] == "evicted"


def test_notes_attach_to_an_already_solved_spec(w):
    spec = {"name": "noted", "deps": {"conda": ["xz >=5"]}}
    first = w.env_ensure(spec)
    again = w.env_ensure({**spec, "notes": ["conductivity sweep baseline"]})
    assert again["env_id"] == first["env_id"]      # identity-neutral
    assert again["status"] == "cached"
    row = w.store.get_env(first["env_id"])
    body = w.store.get_spec(row["spec_hash"])
    assert body["notes"] == ["conductivity sweep baseline"]


def test_relaxed_soft_spec_is_a_cache_hit_next_time(w):
    spec = {"name": "soft", "deps": {"conda": ["xz >=5", "zlib ==0.0.999?"]}}
    first = w.env_ensure(spec, relax="soft")
    assert "env_id" in first, first
    assert first.get("relaxed"), "the soft pin should have been relaxed"
    again = w.env_ensure(spec, relax="soft")
    assert again["status"] == "cached"
    assert again["env_id"] == first["env_id"]


def test_find_near_reports_version_mismatches(w):
    w.env_ensure(PY_BASE)
    near = w.env_find_near({"name": "probe",
                            "deps": {"conda": ["python =3.10"]}})
    assert near, "the 3.12 env is near — one mismatched pin away"
    mm = near[0]["version_mismatches"]
    assert mm and mm[0]["package"] == "python"
    assert mm[0]["want"] == "=3.10" and mm[0]["have"].startswith("3.12")
    assert near[0]["distance"] >= 1


def test_archive_is_honest_about_post_install(w, pixi_bin):
    from pathlib import Path
    if not (Path(pixi_bin).parent / "pixi-pack").exists():
        pytest.skip("pixi-pack not installed")
    env = w.env_ensure({"name": "hatchy", "deps": {"conda": ["xz >=5"]},
                        "post_install": ["echo done > .weft-hatch"]})["env_id"]
    _run(w, env)
    ev = w.env_evict(env, "local", archive=True)
    assert ev["state"] == "evicted"
    assert ev.get("archive_caveats"), ev
    assert "NOT fully offline" in ev["rebuild"]


def test_post_install_delta_disqualifies_the_overlay(w):
    """extends_env + new post_install steps: the products of those steps
    live nowhere in the parent's prefix — a full prefix is the only honest
    realization."""
    parent = w.env_ensure(PY_BASE)["env_id"]
    _run(w, parent)
    child = w.env_ensure({"name": "hatched", "extends_env": parent,
                          "deps": {"pypi": ["emcee"]},
                          "post_install": ["touch .weft-hatched"]})
    assert "env_id" in child, child
    _run(w, child["env_id"], "python -c 'import emcee'")
    real = w.store.get_realization(child["env_id"], "local")
    assert real["strategy"] == "prefix"      # not overlay
