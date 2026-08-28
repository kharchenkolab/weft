"""Wait-legibility round (aba serialization report, 2026-08-27): a
sibling caller queued behind a long environment operation must never be
indistinguishable from a slow verb.

The incident: one thread's session_install / first realize made a
second thread's trivial call read as FROZEN for the operation's whole
duration — the sync build-lock join was silent, env_status said
"building" with no since, the ensure-claim refusal aged only its
heartbeat, and kernel_poll answered "still executing" for a block the
driver (parked in activation) had never picked up. Every wait keeps
its semantics; every wait now says what it waits on and since when."""

import json
import threading
import time

from helpers_verify import ENV, cold_session
from weft import realize
from weft.errors import WeftError


# ── the build-lock join narrates ───────────────────────────────────────────

def test_joined_build_lock_emits_waiting_with_build_start(tmp_path,
                                                          pixi_bin):
    w, _sid = cold_session(tmp_path, pixi_bin)
    w.store.set_realization(ENV, "local", "prefix", "envs/x", "building")
    row = w.store.get_realization(ENV, "local")
    lk = realize._build_lock(ENV, "local")
    assert lk.acquire(blocking=False), "test owns the lock first"
    entered = threading.Event()

    def joiner():
        with realize._joined_build_lock(ENV, w.adapters["local"],
                                        w.store):
            entered.set()

    t = threading.Thread(target=joiner, daemon=True)
    t.start()
    time.sleep(0.3)
    assert not entered.is_set(), "the join must WAIT (sync contract)"
    evs = [e for e in w.store.events_since(0, 200)
           if e["kind"] == "realize.waiting"]
    assert evs, "the wait must be narrated BEFORE blocking"
    assert evs[0]["env_id"] == ENV and evs[0]["site"] == "local"
    assert evs[0]["build_started_at"] == row["updated_at"], \
        "the event names the in-flight build's start, not the joiner's"
    lk.release()
    assert entered.wait(2), "release must unblock the joiner"
    t.join(2)


def test_uncontended_lock_stays_silent(tmp_path, pixi_bin):
    w, _sid = cold_session(tmp_path, pixi_bin)
    with realize._joined_build_lock(ENV, w.adapters["local"], w.store):
        pass
    assert not [e for e in w.store.events_since(0, 200)
                if e["kind"] == "realize.waiting"], \
        "no contention, no noise"


def test_public_realize_join_narrates_then_completes(tmp_path,
                                                     pixi_bin):
    """The consumer-visible shape: a sync env_realize arriving mid-
    build blocks (by contract), the feed says so, and the holder's
    release fast-paths it to ready."""
    w, _sid = cold_session(tmp_path, pixi_bin)
    # own env on the site's REAL platform (the platform gate sits
    # before the lock) with a ready realization + marker for the
    # post-release fast path
    plat = realize._site_platform((w.store.get_site("local") or {})
                                  .get("capabilities") or {})
    env2 = "env:v1:feedwait0001"
    w.store.put_env(env2, "spec_feedwait0001", {
        "extras": {},
        "platforms": {plat: [{"kind": "pypi", "name": "statpack",
                              "version": "1.0"}]},
    }, "lock: {}", "[workspace]", [plat])
    rel2 = realize.env_dir_rel(env2)
    d = tmp_path / "site" / rel2
    d.mkdir(parents=True)
    (d / "activate.sh").write_text("export WEFT_T=1\n")
    (d / ".weft-ready").write_text(json.dumps({"strategy": "prefix"}))
    w.store.set_realization(env2, "local", "prefix", rel2, "ready")
    lk = realize._build_lock(env2, "local")
    assert lk.acquire(blocking=False)
    done = {}

    def sibling():
        done["r"] = w.env_realize(env2, "local")

    t = threading.Thread(target=sibling, daemon=True)
    t.start()
    time.sleep(0.4)
    assert "r" not in done, "sync join waits"
    assert [e for e in w.store.events_since(0, 300)
            if e["kind"] == "realize.waiting"]
    lk.release()
    t.join(5)
    assert done["r"].get("state") in ("ready", None) \
        or "error" not in done["r"], done.get("r")


# ── env_status says since WHEN ─────────────────────────────────────────────

def test_env_status_building_since(tmp_path, pixi_bin):
    w, _sid = cold_session(tmp_path, pixi_bin)
    w.store.set_realization(ENV, "local", "prefix", "envs/x", "building")
    row = w.store.get_realization(ENV, "local")
    ent = next(r for r in w.env_status(ENV)["realizations"]
               if r["site"] == "local")
    assert ent["state"] == "building"
    assert ent["building_since"] == row["updated_at"]
    w.store.set_realization(ENV, "local", "prefix", "envs/x", "ready")
    ent = next(r for r in w.env_status(ENV)["realizations"]
               if r["site"] == "local")
    assert "building_since" not in ent, "ready rows carry no clock"


# ── the ensure-claim refusal carries held-since ────────────────────────────

def test_state_conflict_names_held_since(tmp_path, pixi_bin):
    w, sid = cold_session(tmp_path, pixi_bin)
    assert w.store.claim_session_ensure(sid, "holder-nonce")
    out = w.ensure_available({"session": sid}, {"pypi": ["idna"]},
                             verify=False)
    assert out["error"] == "state.conflict"
    assert 0 <= out["hints"]["held_since_s"] < 10, \
        "how long the install has RUN — not just the heartbeat's age"
    assert 0 <= out["hints"]["holder_beat_age_s"] < 10
    assert "ensure_done" in out["hints"]["suggestion"], \
        "the suggestion points at the event that marks the finish"
    w.store.release_session_ensure(sid, "holder-nonce")
    claim = w.store.session_ensure_claim(sid)
    assert claim is None, "release clears the claim AND its clock"


# ── kernel_poll: 'starting' is not 'executing' ─────────────────────────────

def _slow_activation_site(w, tmp_path, pixi_bin, seconds=3):
    row = w.store.get_site("local")
    w.register_site("local", "local",
                    {**row["config"], "site_prelude": f"sleep {seconds}"})


def test_poll_discriminates_driver_startup_from_execution(
        tmp_path, pixi_bin):
    """The incident's poll shape, replayed with the activation stalled
    by a site_prelude sleep (the field case stalls it behind pixi's
    project lock — same door, deterministic here): a block submitted
    while the driver is still ACTIVATING must answer 'starting', not
    the lie 'still executing'; once the driver is up the same poll
    reaches the block honestly."""
    w, _sid = cold_session(tmp_path, pixi_bin)
    _slow_activation_site(w, tmp_path, pixi_bin, seconds=3)
    k = w.kernel_start("local", "python")["kernel_id"]
    try:
        r = w.kernel_exec(k, "x = 1", wait=False)
        early = w.kernel_poll(k, r["block"], timeout=0.5)
        assert early["state"] == "starting", early
        assert "activation" in early["note"], \
            "the note must teach WHY a driver can be slow to start"
        late = w.kernel_poll(k, r["block"], timeout=15)
        assert late["state"] == "done" and late["rc"] == 0, late
        jd = w.store.get_kernel(k)["jobdir"]
        assert w.adapters["local"].file_exists(f"{jd}/driver.ready"), \
            "the driver marks its loop entry"
    finally:
        w.kernel_stop(k)


def test_poll_running_once_driver_is_up(tmp_path, pixi_bin):
    """A genuinely long block on an UP driver still answers 'running'
    — the discrimination must not misfile real execution."""
    w, _sid = cold_session(tmp_path, pixi_bin)
    k = w.kernel_start("local", "python")["kernel_id"]
    try:
        first = w.kernel_exec(k, "warm = 1", wait=True, timeout=15)
        assert first["rc"] == 0
        r = w.kernel_exec(k, "import time; time.sleep(3)", wait=False)
        mid = w.kernel_poll(k, r["block"], timeout=0.5)
        assert mid["state"] == "running", \
            "an up driver executing a long block is RUNNING, not starting"
        assert w.kernel_poll(k, r["block"], timeout=15)["rc"] == 0
    finally:
        w.kernel_stop(k)


# ── kernel_start says an install is in flight ──────────────────────────────

def test_kernel_start_notes_in_flight_install(tmp_path, pixi_bin):
    w, sid = cold_session(tmp_path, pixi_bin)
    assert w.store.claim_session_ensure(sid, "holder-nonce")
    r = w.kernel_start("local", "python", session_id=sid)
    try:
        assert "install_note" in r, r
        assert "FIRST block waits" in r["install_note"]
        evs = [e for e in w.store.events_since(0, 300)
               if e["kind"] == "kernel.waiting_on_install"]
        assert evs and evs[0]["kernel"] == r["kernel_id"]
        assert evs[0]["session"] == sid
        assert evs[0]["install_since"] > 0
    finally:
        w.store.release_session_ensure(sid, "holder-nonce")
        w.kernel_stop(r["kernel_id"])


def test_kernel_start_quiet_without_install(tmp_path, pixi_bin):
    w, sid = cold_session(tmp_path, pixi_bin)
    r = w.kernel_start("local", "python", session_id=sid)
    try:
        assert "install_note" not in r
        assert not [e for e in w.store.events_since(0, 300)
                    if e["kind"] == "kernel.waiting_on_install"]
    finally:
        w.kernel_stop(r["kernel_id"])


# ── driver parity (R/julia run only in the docker lane) ────────────────────

def test_all_three_drivers_mark_loop_entry():
    from pathlib import Path
    d = Path(realize.__file__).parent / "kernels"
    for name in ("driver.py", "driver.R", "driver.jl"):
        src = (d / name).read_text()
        assert "driver.ready" in src, f"{name} lost the marker"
        assert "driver.ready.tmp" in src, \
            f"{name}: polled files publish atomically (tmp+rename)"
