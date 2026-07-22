"""Perf-round cadence tests (aba benchmarks note ask 2): concurrent
session bring-up must not serialize ACROSS sessions (wall ~= max, never
a+b), and a same-env waiter adopts within the lease poll quantum. The
35.7s field stall class: an accidental global lock around materialize
would pass every single-session test and only show here."""

import json
import threading
import time

from weft.adapters.base import ShimResult
from weft.api import Weft
from weft.realize import _SiteLease, env_dir_rel

ENVA = "env:v1:" + "a" * 64
ENVB = "env:v1:" + "b" * 64


def _warm_env(w, tmp_path, env_id):
    w.store.put_env(env_id, "spec_" + env_id[-12:], {
        "extras": {},
        "platforms": {"any": [{"kind": "pypi", "name": "basepkg",
                               "version": "1.0"}]},
    }, "lock: {}", "[project]", ["any"])
    d = tmp_path / "site" / env_dir_rel(env_id)
    (d / "bin").mkdir(parents=True)
    (d / "activate.sh").write_text(f'export PATH="{d}/bin:$PATH"\n')
    (d / ".weft-ready").write_text(json.dumps({"strategy": "prefix"}))
    w.store.set_realization(env_id, "local", "prefix",
                            env_dir_rel(env_id), "ready", read_only=False)


def test_cross_session_bring_up_does_not_serialize(tmp_path, pixi_bin,
                                                   monkeypatch):
    """Two sessions on two DIFFERENT envs, both paying a scripted 1.2s
    clone concurrently: wall must be ~max(a, b). A serialized run is
    >= 2.4s and fails the 2.0s budget."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    _warm_env(w, tmp_path, ENVA)
    _warm_env(w, tmp_path, ENVB)
    sids = [w.session_start(ENVA, "local")["session_id"],
            w.session_start(ENVB, "local")["session_id"]]

    ad = w.adapters["local"]
    orig_cmd = ad.run_cmd

    def slow_clone(script, timeout=120.0):
        if "pixi install" in script:
            time.sleep(1.2)
            return ShimResult(0, "cloned", "")
        if " add --manifest-path" in script:
            return ShimResult(0, "", "")
        if "stat -c %d" in script or "stat -f %d" in script:
            return ShimResult(0, "same", "")
        return orig_cmd(script, timeout=timeout)

    monkeypatch.setattr(ad, "run_cmd", slow_clone)
    monkeypatch.setattr(ad, "run_activated",
                        lambda script, timeout=120.0: slow_clone(script))

    results = {}

    def install(sid):
        results[sid] = w.session_install(sid, conda=["cmake"])

    t0 = time.monotonic()
    threads = [threading.Thread(target=install, args=(sid,))
               for sid in sids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.monotonic() - t0

    for sid in sids:
        assert "error" not in results[sid], results[sid]
        assert w.store.get_session(sid)["materialize_mode"] == "clone"
    # the load-bearing number: two 1.2s clones concurrently. Serialized
    # >= 2.4s; parallel ~1.2s + overhead. Budget leaves 3x overhead room.
    assert wall < 2.0, f"cross-session bring-up serialized: {wall:.2f}s"


def test_same_env_waiter_adopts_within_poll_quantum(tmp_path, pixi_bin,
                                                    monkeypatch):
    """The perf twin of test_concurrent_builders_share_one_build: the
    waiter must ADOPT (never rebuild) and its extra latency beyond the
    builder's finish is bounded by the lease poll quantum, not some
    hidden multiple of it."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    ad = w.adapters["local"]
    rel = env_dir_rel(ENVA)
    d = tmp_path / "site" / rel
    d.mkdir(parents=True)
    monkeypatch.setattr(_SiteLease, "WAIT_S", 0.2)

    BUILD_S = 1.0
    done = {}

    def builder():
        lease = _SiteLease(ad, rel)
        assert lease.acquire_or_adopt() is False    # we hold it: build
        time.sleep(BUILD_S)
        (d / ".weft-ready").write_text('{"strategy": "prefix"}')
        lease.release()
        done["built_at"] = time.monotonic()

    def waiter():
        time.sleep(0.2)                             # arrive second
        lease = _SiteLease(ad, rel)
        adopted = lease.acquire_or_adopt()
        done["adopted"] = adopted
        done["adopted_at"] = time.monotonic()
        if not adopted:                             # must never happen
            lease.release()

    a = threading.Thread(target=builder)
    b = threading.Thread(target=waiter)
    a.start(); b.start(); a.join(); b.join()

    assert done["adopted"] is True                  # dedup: never rebuilt
    lag = done["adopted_at"] - done["built_at"]
    # adoption lands within ~2 poll quanta of readiness (0.2s patched);
    # budget 3x for fs/exec overhead per poll turn
    assert lag < 1.2, f"adoption lagged readiness by {lag:.2f}s"
    assert not (tmp_path / "site" / (rel + ".lease")).exists()
