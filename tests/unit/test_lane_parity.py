"""bug5 parity sweep — the fix-forward cells, pinned.

bug5 generalized to "a capability wired into ONE lane of a family
while siblings lack it"; the sweep matrix (misc/lane_parity) found
the sibling cells this file pins:

- session cran budget matches the solver lane's worst case (the
  loadcheck arm can rebuild the whole delta from source) and honors
  policy max_build_cores instead of a hardcoded cap;
- the pixi-add session lane persists its logs (both attempts) and
  wraps transport deaths with context — it was the last install lane
  raising with only an in-memory tail;
- the julia overlay actually SPLICES the toolchain prelude it was
  handed (accepted-and-discarded: a deps/build.jl compile ran bare);
- source drift-catchers for the offline cran pack lane's evidence
  and the pypi overlay's syslib classifier (their behavioral drivers
  live in the docker/solver lanes)."""

import inspect

from helpers_verify import ENV, cold_session, script_log
from weft.adapters.base import ShimResult
from weft.realize import env_dir_rel

TC_MARK = "/fake/tc/.pixi/envs/default/bin"


def fake_toolchain(monkeypatch):
    import weft.toolchain as tc
    monkeypatch.setattr(tc, "ensure_toolchain",
                        lambda *a, **k: "/fake/tc")


def _capture_activated(monkeypatch, w, key, result=ShimResult(0, "ok", "")):
    """Record (script, timeout) for run_activated scripts containing
    key; everything else passes through (script_log drops the timeout,
    which THESE pins are about)."""
    ad = w.adapters["local"]
    orig = ad.run_activated
    seen = []

    def route(script, timeout=120.0):
        if key in script:
            seen.append((script, timeout))
            return result
        return orig(script, timeout=timeout)

    monkeypatch.setattr(ad, "run_activated", route)
    return seen


def test_session_cran_budget_and_policy_cap(tmp_path, pixi_bin,
                                            monkeypatch):
    """10800s like the solver lane (the loadcheck arm's source-rebuild
    worst case), and policy max_build_cores reaches the script."""
    w, sid = cold_session(tmp_path, pixi_bin)
    import weft.toolchain as tc
    monkeypatch.setattr(tc, "ensure_toolchain", lambda *a, **k: None)
    row = w.store.get_site("local")
    w.register_site("local", "local",
                    {**row["config"], "policy": {"max_build_cores": 3}})
    seen = _capture_activated(monkeypatch, w, "-cran-install.log")
    _capture_missing = script_log(monkeypatch, w,
                                  {"MISSING:": ShimResult(0, "", "")})
    out = w.session_install(sid, cran=["statgraphs"])
    assert "error" not in out, out
    script, timeout = seen[0]
    assert timeout == 10800, \
        "the budget must cover the loadcheck arm's full source rebuild"
    assert "WEFT_NCPU<3?WEFT_NCPU:3" in script, \
        "policy max_build_cores was bypassed by a hardcoded cap"


def _warm_session(tmp_path, pixi_bin):
    w, sid = cold_session(tmp_path, pixi_bin)
    w.store.set_realization(ENV, "local", "prefix", env_dir_rel(ENV),
                            "ready", read_only=False)
    w.store.set_session_materialized(sid, mode="clone")
    return w, sid


def test_pixi_add_lane_persists_both_logs_and_retries(tmp_path, pixi_bin,
                                                      monkeypatch):
    w, sid = _warm_session(tmp_path, pixi_bin)
    fake_toolchain(monkeypatch)
    gxx = "error: [Errno 2] No such file or directory: 'g++'"
    calls = []
    ad = w.adapters["local"]
    orig = ad.run_cmd

    def route(script, timeout=120.0):
        if "#method" in script:
            return ShimResult(1, "#method none", "")
        if "-pixi-add" in script:            # both wrapped log paths
            calls.append((script, timeout))
            return (ShimResult(1, gxx, "") if len(calls) == 1
                    else ShimResult(0, "", ""))
        return orig(script, timeout=timeout)

    monkeypatch.setattr(ad, "run_cmd", route)
    out = w.session_install(sid, pypi=["statpack"])
    assert "error" not in out, out
    assert len(calls) == 2
    assert "-pixi-add.log" in calls[0][0], \
        "first attempt persists a log — it was the last lane raising " \
        "with only an in-memory tail"
    assert "-pixi-add-tc.log" in calls[1][0] and TC_MARK in calls[1][0]
    assert calls[0][1] == calls[1][1] == 1800, \
        "sdist-capable lanes share the sdist budget (900 predated the arm)"


def test_pixi_add_failure_carries_log_path(tmp_path, pixi_bin,
                                           monkeypatch):
    w, sid = _warm_session(tmp_path, pixi_bin)
    ad = w.adapters["local"]
    orig = ad.run_cmd

    def route(script, timeout=120.0):
        if "#method" in script:
            return ShimResult(1, "#method none", "")
        if "-pixi-add" in script:
            return ShimResult(1, "Cannot solve the request: no "
                                 "candidates were found for statpack",
                              "")
        return orig(script, timeout=timeout)

    monkeypatch.setattr(ad, "run_cmd", route)
    out = w.session_install(sid, pypi=["statpack"])
    assert "error" in out
    assert "log_path" in out["hints"], \
        "evidence outlives the process on this lane too"


def test_julia_overlay_splices_the_prelude(tmp_path, pixi_bin,
                                           monkeypatch):
    """The prelude was ACCEPTED and silently discarded — a provisioned
    capability dropped on the floor (a build.jl compile ran bare on
    compiler-less sites). Activation first, prelude after."""
    w, _sid = cold_session(tmp_path, pixi_bin)
    from weft.solvers import JuliaSolver
    seen = _capture_activated(monkeypatch, w, "-julia-overlay.log")
    prelude = f'export PATH="{TC_MARK}:$PATH"\nexport CPPFLAGS="-I/x"\n'
    line = JuliaSolver.realize_overlay(
        JuliaSolver.__new__(JuliaSolver),
        {"native": 'name = "t"\n###WEFT-MANIFEST###\n'},
        None, ["tpkg"], w.adapters["local"], "envs/julia-t",
        "envs/deadbeefcafe", prelude, {}, ENV)
    script, _ = seen[0]
    assert TC_MARK in script, "the prelude must reach the instantiate"
    assert script.index("activate.sh") < script.index(TC_MARK), \
        "activation FIRST, prelude AFTER (the PATH-reset catch)"
    assert "JULIA_PROJECT" in line


def test_offline_cran_pack_gained_its_capabilities():
    """Drift-catcher (behavioral driver is the docker/solver lane):
    the most compile-heavy lane in the tree — every package a source
    tarball — now persists evidence, exports build parallelism, and
    classifies syslib failures like both its siblings."""
    from weft.solvers import CranSolver
    src = inspect.getsource(CranSolver.pack_layer)
    assert "run_logged" in src and "failure_evidence" in src
    assert "_build_jobs_prefix" in src
    assert "_syslib_hints" in src


def test_overlay_pypi_attaches_syslib_classifier():
    """Drift-catcher: the cran sibling of the SAME build attaches the
    classifier; a missing lzma.h in an overlay wheel build was an
    unclassified failure."""
    from weft import realize
    src = inspect.getsource(realize._overlay_pypi)
    assert "_syslib_hints" in src
