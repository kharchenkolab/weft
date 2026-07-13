"""L3/L4: overlay realization — O(delta) on a parent's prefix, with the
conformance property that makes it an *invisible* optimization."""

import time

import pytest

from weft.api import Weft

pytestmark = [pytest.mark.solver, pytest.mark.slow]

PY_BASE = {"name": "pybase", "deps": {"conda": ["python =3.12", "pip"]}}
R_BASE = {"name": "rbase", "deps": {"conda": ["r-base =4.4"],
                                    "cran": ["jsonlite"]},
          "system_requirements": {"cran_snapshot": "2026-07-01"}}


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def _realize(w, env_id, cmd="true"):
    j = w.runner.wait(w.task_submit({"command": cmd, "env": env_id,
                                     "site": "local"}, force=True)["job_id"],
                      2400)
    assert j["state"] == "DONE", j["error"]
    return j


def test_pypi_overlay_reuses_the_parent_prefix(w):
    parent = w.env_ensure(PY_BASE)["env_id"]
    _realize(w, parent)
    child = w.env_ensure({"name": "pybase+emcee", "extends_env": parent,
                          "deps": {"pypi": ["emcee"]}})
    assert child["delta"]["layerable"] is True

    t0 = time.time()
    j = _realize(w, child["env_id"],
                 "python -c 'import emcee, numpy; print(emcee.__version__)'")
    overlay_s = time.time() - t0
    real = w.store.get_realization(child["env_id"], "local")
    assert real["strategy"] == "overlay"

    # the child's own dir holds ONLY the delta — the parent's bytes are reused
    site_root = w.workspace.parent / "site"
    from weft.realize import env_dir_rel
    child_dir = site_root / env_dir_rel(child["env_id"])
    parent_dir = site_root / env_dir_rel(parent)
    assert not (child_dir / ".pixi").exists()      # no second prefix
    assert (child_dir / "pylib").exists()
    child_bytes = sum(f.stat().st_size for f in child_dir.rglob("*")
                      if f.is_file())
    parent_bytes = sum(f.stat().st_size for f in parent_dir.rglob("*")
                       if f.is_file())
    # the delta (emcee + its numpy dep) must be far from a full second env
    # (parent + delta). The exact ratio is platform-sensitive — osx-arm64
    # ships a much leaner python than linux-64, so numpy/python is larger
    assert child_bytes < parent_bytes / 2, (child_bytes, parent_bytes)

    kinds = [e["kind"] for e in w.events_poll(0, 900, compact=False)["events"]]
    assert "realize.overlay" in kinds and "realize.overlay.done" in kinds
    assert overlay_s < 240


def test_overlay_and_full_prefix_are_indistinguishable(w):
    """THE conformance property: the same EnvID realized as an overlay and as
    a full prefix must produce byte-identical outputs. Overlay is an
    optimization, not a semantic fork."""
    parent = w.env_ensure(PY_BASE)["env_id"]
    _realize(w, parent)
    child = w.env_ensure({"name": "conf", "extends_env": parent,
                          "deps": {"pypi": ["emcee"]}})["env_id"]

    task = {"command": "python -c \"import emcee, sys, json; "
                       "json.dump({'v': emcee.__version__, "
                       "'py': sys.version_info[:2]}, "
                       "open('results/o.json','w'))\"",
            "env": child, "outputs": ["results/"], "site": "local"}
    j_overlay = w.runner.wait(w.task_submit(task, force=True)["job_id"], 2400)
    assert j_overlay["state"] == "DONE", j_overlay["error"]
    assert w.store.get_realization(child, "local")["strategy"] == "overlay"
    ref_overlay = next(o["ref"] for o in j_overlay["manifest"]["outputs"]
                       if o["path"] == "results/o.json")

    # force the same EnvID to realize the old way
    w.env_repair(child, "local")
    w.store.set_env_parent(child, "", layerable=False)   # deny the overlay
    j_full = w.runner.wait(w.task_submit(task, force=True)["job_id"], 2400)
    assert j_full["state"] == "DONE", j_full["error"]
    assert w.store.get_realization(child, "local")["strategy"] == "prefix"
    ref_full = next(o["ref"] for o in j_full["manifest"]["outputs"]
                    if o["path"] == "results/o.json")

    assert ref_overlay == ref_full        # byte-identical, by content hash


def test_conda_delta_falls_back_to_a_full_prefix(w):
    parent = w.env_ensure(PY_BASE)["env_id"]
    _realize(w, parent)
    child = w.env_ensure({"name": "needs-conda", "extends_env": parent,
                          "deps": {"conda": ["xz >=5"]}})
    assert child["delta"]["layerable"] is False
    _realize(w, child["env_id"], "xz --version")
    assert w.store.get_realization(child["env_id"], "local")["strategy"] \
        == "prefix"


def test_cran_overlay_with_compile_cache(w):
    parent = w.env_ensure(R_BASE)["env_id"]
    _realize(w, parent, "Rscript -e 'library(jsonlite)'")

    # NOTE no cran_snapshot here: the child must inherit the parent's
    # (a re-solve at "today" would move the parent's R packages — the
    # silent base drift the reviewers caught)
    child = w.env_ensure({"name": "rbase+glue", "extends_env": parent,
                          "deps": {"cran": ["glue"]}})
    assert child["delta"]["layerable"] is True
    assert child["delta"]["layers_added"]["cran"] == ["glue"]
    child_layer = w.store.get_env(child["env_id"])["canonical"]["layers"]["cran"]
    assert "2026-07-01" in child_layer["snapshot"]     # inherited, not today

    _realize(w, child["env_id"], "Rscript -e 'cat(glue::glue(\"ok-{1+1}\"))'")
    real = w.store.get_realization(child["env_id"], "local")
    assert real["strategy"] == "overlay"

    # the parent's library is still visible through R_LIBS (no shadowing)
    j2 = w.runner.wait(w.task_submit({
        "command": "Rscript -e 'library(jsonlite); library(glue); "
                   "cat(toJSON(1:2))' > results/j.txt",
        "env": child["env_id"], "outputs": ["results/"], "site": "local"},
        force=True)["job_id"], 2400)
    assert j2["state"] == "DONE", j2["error"]
    out = next(o for o in j2["manifest"]["outputs"] if o["path"] == "results/j.txt")
    assert out["preview"]["lines"] == ["[1,2]"]

    # the layer install was cached; a repair + re-realize pays only an untar
    kinds = [e["kind"] for e in w.events_poll(0, 900, compact=False)["events"]]
    assert "overlay.compile_cached" in kinds
    w.env_repair(child["env_id"], "local")
    _realize(w, child["env_id"], "Rscript -e 'cat(glue::glue(\"ok-{1+1}\"))'")
    kinds = [e["kind"] for e in w.events_poll(0, 900, compact=False)["events"]]
    assert "overlay.compile_cache_hit" in kinds
    assert w.store.get_realization(child["env_id"], "local")["strategy"] \
        == "overlay"


def test_github_source_delta_builds_with_the_weft_toolchain(w):
    """A source-built delta (GitHub R package) must NOT pull a compiler into
    the env — weft's own build-time toolchain does the compile, against the
    parent's headers/libs, and the toolchain never appears at runtime."""
    parent = w.env_ensure(R_BASE)["env_id"]
    _realize(w, parent)
    child = w.env_ensure({"name": "rbase+gh", "extends_env": parent,
                          "deps": {"cran": ["tidyverse/glue@main"]},
                          "system_requirements": {"cran_snapshot": "2026-07-01"}})
    assert child["delta"]["layerable"] is True

    j = w.runner.wait(w.task_submit({
        "command": "Rscript -e 'cat(glue::glue(\"gh-{2*2}\"))' "
                   "> results/o.txt && command -v cc > results/cc.txt "
                   "|| true",
        "env": child["env_id"], "outputs": ["results/"], "site": "local"},
        force=True)["job_id"], 2400)
    assert j["state"] == "DONE", j["error"]
    assert w.store.get_realization(child["env_id"], "local")["strategy"] \
        == "overlay"
    out = next(o for o in j["manifest"]["outputs"]
               if o["path"] == "results/o.txt")
    assert out["preview"]["lines"] == ["gh-4"]
    # the toolchain is build-time only: no weft compiler on the runtime PATH
    cc = next((o for o in j["manifest"]["outputs"]
               if o["path"] == "results/cc.txt"), None)
    if cc and cc["preview"]["lines"]:
        assert "toolchain" not in cc["preview"]["lines"][0]


def test_github_parent_pins_by_sha_in_children(w):
    """A github package in the parent must stay THAT build in every child —
    inheriting a bare name would silently substitute the same-versioned
    CRAN release; inheriting @main would follow the branch."""
    parent = w.env_ensure({"name": "gh-parent",
                           "deps": {"conda": ["r-base =4.4"],
                                    "cran": ["tidyverse/glue@main"]},
                           "system_requirements": {
                               "cran_snapshot": "2026-07-01"}})["env_id"]
    p_rec = next(r for r in w.store.get_env(parent)["canonical"]["layers"]
                 ["cran"]["records"] if r["name"] == "glue")
    assert p_rec.get("remote_sha")

    # the delta is another R package (a pypi delta would rightly conflict:
    # this parent has no python — the hint names that lever)
    child = w.env_ensure({"name": "gh-child", "extends_env": parent,
                          "deps": {"cran": ["R6"]}})
    assert "env_id" in child, child
    assert child["delta"]["layerable"] is True
    assert child["delta"]["layers_added"]["cran"] == ["R6"]
    c_rec = next(r for r in w.store.get_env(child["env_id"])["canonical"]
                 ["layers"]["cran"]["records"] if r["name"] == "glue")
    # same artifact: same commit, still a github source
    assert c_rec.get("remote_sha") == p_rec["remote_sha"]
    assert c_rec["source"].startswith("github:")

    # a pypi delta on this python-less parent conflicts — and the hint
    # names the real lever (add python to the delta), not a re-solve
    r = w.env_ensure({"name": "gh-pypi", "extends_env": parent,
                      "deps": {"pypi": ["emcee"]}})
    assert r["error"] == "env.layer_conflict"
    assert "add \"python\"" in r["hints"]["suggestion"]


def test_verification_failure_falls_back_to_a_full_prefix(w, monkeypatch):
    """The safety valve: if the composed env fails its load checks, the
    overlay is abandoned and the SAME EnvID realizes as a full prefix —
    the task never sees the failed experiment."""
    parent = w.env_ensure(PY_BASE)["env_id"]
    _realize(w, parent)
    child = w.env_ensure({"name": "fallback", "extends_env": parent,
                          "deps": {"pypi": ["emcee"]}})["env_id"]

    import weft.realize as realize_mod

    def broken_verify(*a, **k):
        raise realize_mod.WeftError("env.realize_failed",
                                    "simulated composition failure",
                                    stage="realize")
    monkeypatch.setattr(realize_mod, "_verify_overlay", broken_verify)
    j = _realize(w, child, "python -c 'import emcee'")
    assert j["state"] == "DONE"
    assert w.store.get_realization(child, "local")["strategy"] == "prefix"
    events = w.events_poll(0, 900, compact=False)["events"]
    fb = next(e for e in events if e["kind"] == "realize.overlay_fallback")
    assert "simulated composition failure" in fb["reason"]


def test_evicting_a_parent_with_live_overlays_refuses_then_cascades(w):
    """Reclaiming the parent's GBs must not silently break the children
    stacked on it: refuse with the dependents named, cascade on request."""
    parent = w.env_ensure(PY_BASE)["env_id"]
    _realize(w, parent)
    child = w.env_ensure({"name": "dep", "extends_env": parent,
                          "deps": {"pypi": ["emcee"]}})["env_id"]
    _realize(w, child, "python -c 'import emcee'")

    r = w.env_evict(parent, "local")
    assert r["error"] == "env.evict_blocked"
    assert child in r["hints"]["dependents"]
    assert "cascade" in r["hints"]["suggestion"]
    # still intact after the refusal
    assert w.store.get_realization(child, "local")["state"] == "ready"

    # an overlay child alone evicts freely — it owns only its delta
    r2 = w.env_evict(child, "local")
    assert r2["state"] == "evicted"
    assert w.store.get_realization(parent, "local")["state"] == "ready"
    # and comes back as an overlay (the parent is still here)
    _realize(w, child, "python -c 'import emcee'")
    assert w.store.get_realization(child, "local")["strategy"] == "overlay"

    r = w.env_evict(parent, "local", cascade=True)
    assert r["state"] == "evicted"
    assert [c["env_id"] for c in r["cascaded"]] == [child]
    assert w.store.get_realization(child, "local")["state"] == "evicted"

    # with the parent gone, the same EnvID comes back as a FULL prefix —
    # an honest strategy switch, not a broken overlay
    j = _realize(w, child, "python -c 'import emcee'")
    assert j["state"] == "DONE"
    assert w.store.get_realization(child, "local")["strategy"] == "prefix"


def test_session_on_an_overlay_env(w):
    """Interactive sessions — the whole reason layering exists — run on an
    overlay-realized env like on any other."""
    parent = w.env_ensure(PY_BASE)["env_id"]
    _realize(w, parent)
    child = w.env_ensure({"name": "interactive", "extends_env": parent,
                          "deps": {"pypi": ["emcee"]}})["env_id"]

    s = w.session_start(child, "local")
    sid = s["session_id"]
    try:
        r = w.session_exec(sid, "python -c 'import emcee; "
                                "print(emcee.__version__)'")
        assert r["rc"] == 0, r
        assert w.store.get_realization(child, "local")["strategy"] \
            == "overlay"
    finally:
        w.session_stop(sid)


def test_parent_tamper_rebuilds_the_child(w):
    """The integrity fence is two deep: touch the parent, the child rebuilds
    rather than running against a changed base."""
    parent = w.env_ensure(PY_BASE)["env_id"]
    _realize(w, parent)
    child = w.env_ensure({"name": "fence", "extends_env": parent,
                          "deps": {"pypi": ["emcee"]}})["env_id"]
    _realize(w, child, "python -c 'import emcee'")

    from weft.realize import env_dir_rel
    site_root = w.workspace.parent / "site"
    victim = (site_root / env_dir_rel(parent) / ".pixi" / "envs" / "default"
              / "bin" / "pip")
    if victim.exists():
        victim.unlink()

    j = _realize(w, child, "python -c 'import emcee'")
    kinds = [e["kind"] for e in w.events_poll(0, 900, compact=False)["events"]]
    assert "realize.parent_changed" in kinds
    assert j["state"] == "DONE"
