"""Session lifecycle round (aba session note): fast pypi installs
(no per-add re-solve), last_used/idle_s facts, dead-record
reconciliation in gc_orphans, and the opt-in session_idle_days policy.
Sessions here are fabricated store records — the solve-dependent
end-to-end lives in the solver lane (test_session_ergonomics)."""

import time

import pytest

from weft.api import Weft
from weft.errors import WeftError

ENV = "env:v1:" + "a" * 64
CANON = {"extras": {}, "platforms": {"linux-64": []}, "version": 2}


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def _session(w, sid="ses_test1", loc="sessions/ses_test1"):
    w.store.put_session(sid, ENV, "local", loc)
    return sid


def _age(w, sid, seconds):
    w.store._write("UPDATE sessions SET last_used=? WHERE session_id=?",
                   (time.time() - seconds, sid))


class FakeShim:
    def __init__(self, rc=0, out="", err=""):
        self.rc, self.out, self.err = rc, out, err


class FakeAdapter:
    """Answers keyed by substring; records the command stream."""
    name, root = "local", "/site"

    def __init__(self, answers):
        self.answers, self.commands = answers, []

    def path(self, rel):
        return rel if rel.startswith("/") else f"{self.root}/{rel}"

    @property
    def pixi_bin(self):
        return self.path("bin/pixi")

    def run_cmd(self, script, *, timeout=120.0):
        self.commands.append(script)
        for key, resp in self.answers.items():
            if key in script:
                return resp
        return FakeShim()


# -- fast pypi path -------------------------------------------------------

def test_fast_pypi_install_skips_the_solve(w):
    sid = _session(w)
    fake = FakeAdapter({"#method": FakeShim(out="#method uv\n")})
    out = w.sessions.install(sid, fake, pypi=["six"])
    assert out["solved"] is False and out["method"] == "uv"
    assert out["verified_at"] == "snapshot"
    # the whole point: no manifest re-solve happened
    assert not any("pixi" in c and " add " in f" {c} "
                   for c in fake.commands), fake.commands
    # the dep is still RECORDED — snapshot's solve mints identity
    assert w.store.get_session(sid)["added_pypi"] == ["six"]


def test_fast_failure_falls_through_to_the_solve(w):
    sid = _session(w, "ses_ff", "sessions/ses_ff")
    fake = FakeAdapter({
        "#method": FakeShim(rc=1, out="#method uv\n",
                            err="No solution: six conflicts"),
        " add ": FakeShim(rc=0),
    })
    out = w.sessions.install(sid, fake, pypi=["six"])
    assert "error" not in out
    assert "conflicts" in out["fast_fallback"]
    assert any("pixi" in c and "--pypi" in c for c in fake.commands)
    assert w.store.get_session(sid)["added_pypi"] == ["six"]


def test_no_direct_tool_falls_through_silently(w):
    sid = _session(w, "ses_nt", "sessions/ses_nt")
    fake = FakeAdapter({
        "#method": FakeShim(rc=87, out="#method none\n"),
        " add ": FakeShim(rc=0),
    })
    out = w.sessions.install(sid, fake, pypi=["six"])
    assert "error" not in out and "fast_fallback" not in out


def test_conda_and_fast_false_solve_at_add(w):
    sid = _session(w, "ses_cd", "sessions/ses_cd")
    fake = FakeAdapter({" add ": FakeShim(rc=0)})
    out = w.sessions.install(sid, fake, conda=["xz"], pypi=["six"])
    assert "solved" not in out             # classic path
    assert not any("#method" in c for c in fake.commands)
    fake2 = FakeAdapter({" add ": FakeShim(rc=0)})
    sid2 = _session(w, "ses_cf", "sessions/ses_cf")
    out = w.sessions.install(sid2, fake2, pypi=["six"], fast=False)
    assert not any("#method" in c for c in fake2.commands)


# -- last_used / idle_s ---------------------------------------------------

def test_session_verbs_touch_last_used_and_listing_reports_idle(w):
    sid = _session(w)
    _age(w, sid, 7200)
    assert w.list_sessions("local")[0]["idle_s"] >= 7100
    # any session verb refreshes the activity fact (exec fails on the
    # fabricated prefix — the touch happens on entry regardless)
    w.session_exec(sid, "true")
    row = w.list_sessions("local")[0]
    assert row["idle_s"] < 60 and row["has_kernel"] is False
    assert row["state"] == "active"


def test_evict_refusal_names_idle_holders(w):
    w.store.put_env(ENV, "s" * 64, CANON, "lock: 1\n", "[workspace]\n",
                    ["linux-64"])
    w.store.set_realization(ENV, "local", "prefix", "envs/" + "a" * 64,
                            "ready")
    sid = _session(w)
    _age(w, sid, 3 * 86400)
    out = w.env_evict(ENV, "local")
    assert out["error"] == "env.evict_blocked"
    holder = out["hints"]["in_use"][0]
    assert holder["kind"] == "session" and holder["id"] == sid
    assert holder["idle_s"] >= 3 * 86400 - 60
    assert holder["has_kernel"] is False


# -- dead-record reconciliation -------------------------------------------

def test_gc_orphans_retires_dead_session_records(w, tmp_path):
    dead = _session(w, "ses_dead", "sessions/ses_dead")      # no dir
    alive = _session(w, "ses_alive", "sessions/ses_alive")
    (tmp_path / "site" / "sessions" / "ses_alive").mkdir(parents=True)
    out = w.gc_orphans("local")                  # plan mode reconciles too
    assert out["dead_session_records"] == [dead]
    assert w.store.get_session(dead)["state"] == "stopped"
    assert w.store.get_session(alive)["state"] == "active"
    ev = [e for e in w.events_poll(0, 500)["events"]
          if e["kind"] == "session.reaped"]
    assert ev and "directory gone" in ev[0]["why"]
    # and the unblocking that motivated it: evict now proceeds
    w.store.put_env(ENV, "s" * 64, CANON, "lock: 1\n", "[workspace]\n",
                    ["linux-64"])
    w.store.set_realization(ENV, "local", "prefix", "envs/" + "a" * 64,
                            "ready")
    w.store.set_session_state(alive, "stopped")
    out = w.env_evict(ENV, "local")
    assert "error" not in out, out


# -- session_idle_days policy ---------------------------------------------

def test_idle_policy_defaults_off(w):
    sid = _session(w)
    _age(w, sid, 365 * 86400)
    plan = w.gc_plan("local")["sites"]["local"]
    assert plan["session_idle_days_policy"] is None
    assert plan["idle_sessions"] == []           # a year idle: untouched


def test_idle_policy_plans_and_sweeps_kernel_less_sessions(tmp_path,
                                                           pixi_bin):
    w = Weft(tmp_path / "ws2", pixi_bin=pixi_bin)
    w.register_site("local", "local", {
        "root": str(tmp_path / "site2"), "pixi_source": pixi_bin,
        "policy": {"session_idle_days": 1}})
    for sid in ("ses_old", "ses_fresh", "ses_kern"):
        w.store.put_session(sid, ENV, "local", f"sessions/{sid}")
        (tmp_path / "site2" / "sessions" / sid).mkdir(parents=True)
    _age(w, "ses_old", 2 * 86400)
    _age(w, "ses_kern", 2 * 86400)
    # an attached RUNNING kernel exempts a session no matter how idle
    w.store.put_kernel("krn_x", "local", "python", None,
                       "kernels/krn_x", "h", session_id="ses_kern")

    plan = w.gc_plan("local")["sites"]["local"]
    assert plan["session_idle_days_policy"] == 1
    assert [s["session_id"] for s in plan["idle_sessions"]] == ["ses_old"]
    assert plan["idle_sessions"][0]["idle_days"] >= 1.9

    swept = w.gc_sweep("local", confirm=True)
    assert swept["stopped_idle_sessions"] == 1
    assert w.store.get_session("ses_old")["state"] == "stopped"
    assert w.store.get_session("ses_fresh")["state"] == "active"
    assert w.store.get_session("ses_kern")["state"] == "active"
    assert not (tmp_path / "site2" / "sessions" / "ses_old").exists()
    ev = [e for e in w.events_poll(0, 500)["events"]
          if e["kind"] == "session.reaped"]
    assert ev and "session_idle_days" in ev[0]["why"]
