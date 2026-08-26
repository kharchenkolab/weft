"""Q5 async realize (aba2 Ask 30): env_realize(wait=False) is a
submit-shaped lane — the return describes how to poll, the realization
row is the pollable state, realize.* events narrate, and failure lands
the SAME evidence envelope the blocking lane raises (on the row as
log_tail and on realize.async_failed). Concurrency is join-shaped: a
second submit while a build is in flight joins it instead of stacking
a second build. Ordering is pinned with Events and thread joins, not
sleeps (the machine-cadence rule: loopback timing hides races)."""

import threading
import time

import pytest

from weft.api import Weft
from weft.errors import WeftError

ENV = "env:v1:" + "a" * 64
DEADLINE = 10.0


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.store.put_env(ENV, "spec:x", {"platforms": {}}, "", "", ["any"])
    return w


def _submit(w, site):
    """Submit and capture the build thread so tests can join it —
    row-state polling alone races the post-build event emissions."""
    out = w.env_realize(ENV, site, wait=False)
    th = w._realize_inflight.get((ENV, site))
    return out, th


def _join(th):
    if th is not None:
        th.join(DEADLINE)
        assert not th.is_alive(), "build thread did not finish in time"


def _row(w, site):
    rows = [r for r in w.env_status(ENV)["realizations"]
            if r["site"] == site]
    assert rows, f"no realization row for {site}"
    return rows[0]


def _events(w, kind):
    return [e for e in w.store.events_since(0, limit=2000)
            if e["kind"] == kind]


def _gated_realize(monkeypatch, release: threading.Event,
                   calls: list, fail: Exception | None = None):
    """A fake build that blocks on an Event — ordering by construction,
    not by sleep. Signature mirrors the real ensure_realization."""
    import weft.realize as realize_mod
    from weft.realize import env_dir_rel

    def fake(eid, env_row, adapter, store, caps=None, site_config=None,
             prefer=None, pack_tools=None):
        calls.append(adapter.name)
        assert release.wait(DEADLINE), "test never released the build"
        if fail is not None:
            raise fail
        rel = env_dir_rel(eid)
        adapter.write_file(f"{rel}/.weft-ready", b"{}\n")
        store.set_realization(eid, adapter.name, "prefix", rel, "ready")
        return store.get_realization(eid, adapter.name)

    monkeypatch.setattr(realize_mod, "ensure_realization", fake)


def test_submit_returns_while_build_runs(w, monkeypatch):
    release, calls = threading.Event(), []
    _gated_realize(monkeypatch, release, calls)
    out, th = _submit(w, "local")
    # returned while the build is provably still blocked on the gate
    assert out["state"] == "submitted"
    assert out["process_bound"] is True
    assert out["poll"]["verb"] == "env_status"
    assert set(out["poll"]["terminal"]) == {"ready", "failed"}
    # the recipe for builds that must outlive the controller names the
    # memoization-busting lever (a placebo task without force=True
    # returns the recorded manifest and never rebuilds)
    assert "task_submit" in out["note"] and "force=True" in out["note"]
    assert _events(w, "realize.submitted"), "submit event opens the lane"
    release.set()
    _join(th)
    assert _row(w, "local")["state"] == "ready"
    done = _events(w, "realize.async_done")
    assert done and done[-1]["env_id"] == ENV
    assert calls == ["local"]


def test_second_submit_joins_the_inflight_build(w, monkeypatch):
    release, calls = threading.Event(), []
    _gated_realize(monkeypatch, release, calls)
    first, th = _submit(w, "local")
    second, _ = _submit(w, "local")
    assert first.get("joined") is None
    assert second["state"] == "submitted" and second["joined"] is True
    release.set()
    _join(th)
    assert _row(w, "local")["state"] == "ready"
    assert calls == ["local"], "the joined submit must not build twice"
    # a LATER submit (build done, thread joined) drives the idempotent
    # door again — submit always re-verifies; ensure_realization owns
    # the fast no-op
    release2, calls2 = threading.Event(), []
    release2.set()
    _gated_realize(monkeypatch, release2, calls2)
    third, th3 = _submit(w, "local")
    assert third.get("joined") is None
    _join(th3)
    assert calls2 == ["local"]


def test_failure_lands_evidence_on_row_and_event(w, monkeypatch):
    release, calls = threading.Event(), []
    err = WeftError(
        "env.realize_failed", "pixi install failed on site",
        stage="realize", retryable=True,
        hints={"log_path": "/site/logs/realize-x.log",
               "error_regions": [{"marker": "syslib",
                                  "lines": ["zlib.h: No such file"]}]})
    _gated_realize(monkeypatch, release, calls, fail=err)
    out, th = _submit(w, "local")
    assert out["state"] == "submitted"
    release.set()
    _join(th)
    row = _row(w, "local")
    assert row["state"] == "failed"
    # env_status renders the envelope where the poll contract says
    assert "env.realize_failed" in row["log_tail"]
    ev = _events(w, "realize.async_failed")[-1]
    assert ev["error"] == "env.realize_failed"
    assert ev["retryable"] is True
    assert ev["hints"]["log_path"] == "/site/logs/realize-x.log"
    assert ev["hints"]["error_regions"][0]["marker"] == "syslib"


def test_nonweft_crash_becomes_internal_error_not_silence(w, monkeypatch):
    release, calls = threading.Event(), []
    _gated_realize(monkeypatch, release, calls,
                   fail=ValueError("boom in the build"))
    _, th = _submit(w, "local")
    release.set()
    _join(th)
    assert _row(w, "local")["state"] == "failed"
    ev = _events(w, "realize.async_failed")[-1]
    assert ev["error"] == "internal.error"
    assert "ValueError" in ev["detail"] and "boom" in ev["detail"]


def test_failed_row_is_not_a_wedge(w, monkeypatch):
    """Retry after an async failure rebuilds: 'failed' never gets the
    ready fast-path in ensure_realization, so the next call (blocking
    or async) drives a real build."""
    release, calls = threading.Event(), []
    release.set()
    _gated_realize(monkeypatch, release, calls,
                   fail=WeftError("env.realize_failed", "transient",
                                  stage="realize", retryable=True))
    _, th = _submit(w, "local")
    _join(th)
    assert _row(w, "local")["state"] == "failed"
    ok_release, ok_calls = threading.Event(), []
    ok_release.set()
    _gated_realize(monkeypatch, ok_release, ok_calls)
    out = w.env_realize(ENV, "local")     # blocking retry
    assert out["state"] == "ready"
    assert ok_calls == ["local"]


def test_refusals_stay_synchronous(w):
    """wait=False must not defer intake refusals into the thread: an
    unknown EnvID refuses immediately at the tool boundary (envelope,
    returns-never-raises), exactly like wait=True."""
    out = w.env_realize("env:v1:" + "0" * 64, "local", wait=False)
    assert out["error"] == "task.invalid"
    assert "unknown EnvID" in out["detail"]
    assert not _events(w, "realize.submitted"), \
        "a refused submit must not open the lane"


def test_env_status_renders_sparse_canonical(w):
    """The poll verb must render every ACCEPTED env row: canonicals
    arriving through bundle_import / publish-adopt sidecars carry no
    shape guarantee for extras, and a hard index made env_status answer
    internal.error forever on such rows (found by this suite's forged
    bundle-shaped fixture). Render-tolerance is the contract — the
    overlay extras merge already treated the field as optional; the
    hard index was the second, contradictory implementation."""
    st = w.env_status(ENV)
    assert "error" not in st, st
    assert st["summary"]["modules"] == []
    assert st["realizations"] == []


def test_two_sites_build_concurrently(w, monkeypatch, tmp_path):
    """The ask's actual shape (c9 journey): two sites' builds overlap
    instead of serializing. Both builds sit INSIDE ensure_realization
    at the same moment — impossible in the blocking lane."""
    w.register_site("local2", "local",
                    {"root": str(tmp_path / "site2"),
                     "pixi_source": w.pixi_bin})
    release, calls = threading.Event(), []
    _gated_realize(monkeypatch, release, calls)
    a, th_a = _submit(w, "local")
    b, th_b = _submit(w, "local2")
    assert a["state"] == b["state"] == "submitted"
    assert b.get("joined") is None, "different sites are different builds"
    # both builds reach the gate while neither has finished: overlap
    # proven by state, not sleep
    t0 = time.monotonic()
    while len(calls) < 2 and time.monotonic() - t0 < DEADLINE:
        time.sleep(0.01)
    assert sorted(calls) == ["local", "local2"], calls
    release.set()
    _join(th_a)
    _join(th_b)
    assert _row(w, "local")["state"] == "ready"
    assert _row(w, "local2")["state"] == "ready"
