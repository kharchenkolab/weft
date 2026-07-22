"""Bench lane (aba perf note asks 1, 3, 5, 6): measured per-mechanism
budgets on the LOCAL topology, asserted, with the numbers printed.
Thresholds are 3-5x the measured baselines on a warm mac — generous
enough to survive CI load, tight enough that a mechanism regressing by
its own magnitude (the 10s-clone-that-wasn't-needed class) goes red.

Run: pixi run pytest -m bench -s
(solver lane: real indexes; first run populates the site cache)
"""

import time

import pytest

from weft.api import Weft

pytestmark = [pytest.mark.bench, pytest.mark.solver, pytest.mark.slow]

PY_SPEC = {"name": "bench-py", "deps": {"conda": ["python =3.12", "pip"]}}
R_SPEC = {"name": "bench-r", "deps": {"conda": ["r-base =4.4", "r-jsonlite"]}}


@pytest.fixture(scope="module")
def w(tmp_path_factory, pixi_bin):
    tmp = tmp_path_factory.mktemp("bench")
    w = Weft(tmp / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def _timed(fn, *a, **kw):
    t0 = time.monotonic()
    out = fn(*a, **kw)
    return out, time.monotonic() - t0


def test_bench_python_session_flow(w):
    """The cold-start table for one python session: build, start,
    zero-clone pypi add, satisfied re-ensure, forced clone, kernel
    bring-up. Asserts the numbers aba's end-to-end SLO sits on."""
    env, solve_s = _timed(w.env_ensure, PY_SPEC)
    assert "error" not in env, env
    eid = env["env_id"]

    # realize-at-start: the base builds here (warm site cache after
    # the first bench run; cold on a fresh checkout — print, don't pin)
    s, start_s = _timed(w.session_start, eid, "local")
    assert "error" not in s, s
    sid = s["session_id"]

    # zero-clone regate (ask 6's behavioral guard): pypi-only first add
    ins, ins_s = _timed(w.session_install, sid, pypi=["idna"])
    assert ins.get("mode") == "pylib", ins
    assert ins_s < 30, f"pylib add took {ins_s:.1f}s (budget 30s local)"

    # satisfied re-ensure, pypi lane (ask 1a): pre-check only, local.
    # measured baseline ~0.3-1s -> budget 5s
    env1, first_ensure_s = _timed(
        w.ensure_available, {"session": sid}, {"pypi": ["idna"]})
    assert env1["satisfied"] is True, env1
    assert env1["changed"] is False, env1        # already recorded
    assert first_ensure_s < 5, \
        f"satisfied re-ensure took {first_ensure_s:.1f}s (budget 5s local)"
    # amortization (ask 3): the satisfied pre-check must not re-pay the
    # install (record-gating's perf twin)
    assert first_ensure_s < max(ins_s, 1.0), \
        f"re-ensure {first_ensure_s:.1f}s vs install {ins_s:.1f}s"

    # forced clone (ask 6: materialization budget, reflink-capable
    # local volume, warm cache). aba floor: ~10s at 55-61k entries;
    # this env is far smaller -> 45s is >4x the expected class budget
    up, clone_s = _timed(w.session_install, sid, conda=["xz"],
                         full_clone=True)
    assert "error" not in up, up
    assert clone_s < 45, f"clone+absorb took {clone_s:.1f}s (budget 45s)"

    # kernel bring-up, local topology (ask 5)
    t0 = time.monotonic()
    k = w.kernel_start("local", "python", session_id=sid)
    assert "error" not in k, k
    r = w.kernel_exec(k["kernel_id"], "print(6*7)", timeout=60)
    kernel_s = time.monotonic() - t0
    assert r["rc"] == 0 and "42" in r["out"], r
    assert kernel_s < 30, f"kernel bring-up {kernel_s:.1f}s (budget 30s)"
    w.kernel_stop(k["kernel_id"])
    w.session_stop(sid)

    print(f"\n[bench local/python] solve={solve_s:.1f}s "
          f"realize+start={start_s:.1f}s pylib_add={ins_s:.1f}s "
          f"satisfied_reensure={first_ensure_s:.2f}s "
          f"clone_absorb={clone_s:.1f}s kernel_bringup={kernel_s:.1f}s")


def test_bench_r_satisfied_reensure(w):
    """Ask 1b, measure-first: the R lane's satisfied re-ensure pays a
    FRESH interpreter per oracle (0.5-2s typical) — pin the budget the
    consumer SLO sits on before optimizing anything."""
    env = w.env_ensure(R_SPEC)
    assert "error" not in env, env
    s = w.session_start(env["env_id"], "local")
    assert "error" not in s, s
    sid = s["session_id"]

    # first ensure: jsonlite ships in the base -> verify-only, records
    env1, first_s = _timed(
        w.ensure_available, {"session": sid}, {"cran": ["jsonlite"]})
    assert env1["satisfied"] is True, env1

    # satisfied re-ensure: record present -> pre-check short-circuit
    env2, re_s = _timed(
        w.ensure_available, {"session": sid}, {"cran": ["jsonlite"]})
    assert env2["satisfied"] is True and env2["changed"] is False, env2
    assert env2["attempts"] == [], env2
    # measured baseline: one fresh Rscript oracle 0.5-2s; budget 4x
    assert re_s < 8, f"satisfied R re-ensure {re_s:.1f}s (budget 8s local)"
    # amortization: re-ensure must beat the verify-everything first pass
    assert re_s <= first_s + 0.5, (re_s, first_s)
    w.session_stop(sid)

    print(f"\n[bench local/R] first_ensure={first_s:.1f}s "
          f"satisfied_reensure={re_s:.1f}s")
