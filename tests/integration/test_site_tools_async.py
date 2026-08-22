"""Non-blocking site tools (aba2 registration-latency ask): the ~50 MB
pixi/pixi-unpack push was 33s of a 37s measured registration. It now
runs in the background by default (register_site returns after
shim+probe), and "tool-less is legal" finally became SELF-HEALING: the
build door (ensure_realization) ensures/joins/heals tools at use — a
push cut by a crash, a failed fetch, or tools="skip" all converge at
the first build instead of degrading the site until re-registration
(the pre-round behavior). One push per site per process (the retain
claim pattern); the row remembers what the events narrate."""

import threading
import time
from pathlib import Path

import pytest

from weft.api import Weft
from weft.errors import WeftError
from weft.realize import _ensure_tools_at_use
from weft.site_tools import ensure_site_tools_once, tools_state


@pytest.fixture
def w(tmp_path, pixi_bin):
    return Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")


class FakeAdapter:
    """The seams site_tools/realize touch: root, path, file_exists,
    run_cmd (--version probes), _push_binary. Push latency injectable
    for the cadence test; every call logged."""

    def __init__(self, name="fake", push_delay=0.0, push_error=None):
        self.name, self.root = name, "/fake-root"
        self.push_delay, self.push_error = push_delay, push_error
        self.pushed: list = []
        self.calls: list = []
        self._exists: set = set()
        self._lock = threading.Lock()

    def path(self, rel):
        return f"{self.root}/{rel}"

    def file_exists(self, rel):
        self.calls.append(("file_exists", rel))
        return rel in self._exists

    def run_cmd(self, cmd, timeout=60):
        self.calls.append(("run_cmd", cmd))

        class R:
            rc, out, err = 0, "ok\npixi 0.0", ""
        return R()

    def _push_binary(self, local, rel):
        if self.push_error:
            raise self.push_error
        time.sleep(self.push_delay)
        with self._lock:
            self.pushed.append(rel)
            self._exists.add(rel)


@pytest.fixture
def fake_fetch(monkeypatch, tmp_path):
    fake_bin = tmp_path / "fake-tool"
    fake_bin.write_bytes(b"#!/bin/sh\n")
    import weft.site_tools as st
    monkeypatch.setattr(st, "fetch_tool", lambda tool, plat: fake_bin)
    return fake_bin


def _wait_tools(w, site, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = (w.store.get_site(site) or {}).get("tools") or {}
        if t.get("state") not in (None, "preparing"):
            return t
        time.sleep(0.1)
    raise AssertionError(f"tools never settled: {t}")


def test_register_background_lands_ready(w, tmp_path, pixi_bin):
    out = w.register_site("local", "local", {
        "root": str(tmp_path / "site"), "pixi_source": pixi_bin})
    assert out["tools"]["state"] in ("preparing", "ready")  # racy: fast
    t = _wait_tools(w, "local")
    assert t["state"] == "ready", t
    assert t["detail"]["pixi"] == "ok"                # pixi_source copy
    assert "pixi-unpack" in t["detail"]
    ev = [e for e in w.events_poll(0, 500)["events"]
          if e["kind"] == "site.tools"]
    assert ev and "error" not in ev[-1]
    # the row is queryable state, not just an event
    assert w.sites_list()[0]["tools"] == "ready"
    assert w.sites_describe("local")["tools"]["state"] == "ready"


def test_register_sync_is_ready_on_return(w, tmp_path, pixi_bin):
    out = w.register_site("local", "local", {
        "root": str(tmp_path / "site"), "pixi_source": pixi_bin},
        tools="sync")
    assert out["tools"]["state"] == "ready"           # no preparing window


def test_register_skip_defers_entirely(w, tmp_path, pixi_bin):
    out = w.register_site("local", "local", {
        "root": str(tmp_path / "site"), "pixi_source": pixi_bin},
        tools="skip")
    assert out["tools"]["state"] == "skipped"
    assert not (tmp_path / "site" / "bin" / "pixi-unpack").exists()


def test_tools_vocab_refuses_unknown(w, tmp_path, pixi_bin):
    bad = w.register_site("local", "local", {
        "root": str(tmp_path / "site")}, tools="later")
    assert bad["error"] == "task.invalid"
    assert bad["hints"]["known"] == ["background", "skip", "sync"]


def test_concurrent_ensures_push_once(w, fake_fetch):
    """Machine-cadence: register-then-immediately-realize is the agent
    pattern — the realize-time ensure must JOIN the in-flight push,
    never start a second ~50 MB write."""
    w.store.put_site("fake", "ssh", {})
    ad = FakeAdapter(push_delay=0.3)
    results = []

    def call():
        results.append(
            ensure_site_tools_once(ad, "linux-64", w.store, "fake"))

    t1 = threading.Thread(target=call)
    t2 = threading.Thread(target=call)
    t1.start(); time.sleep(0.05); t2.start()
    t1.join(); t2.join()
    assert len(ad.pushed) == 2                 # pixi + pixi-unpack, ONCE
    assert len(results) == 2 and all(isinstance(r, dict) for r in results)
    assert (w.store.get_site("fake")["tools"] or {})["state"] == "ready"


def test_stale_preparing_heals_at_use(w, fake_fetch):
    """A push cut by process death leaves 'preparing' forever; the
    build door heals it (the pre-round behavior was permanent
    degradation until re-registration)."""
    w.store.put_site("fake", "ssh", {})
    w.store.set_site_tools("fake", {"state": "preparing", "at": 0})
    ad = FakeAdapter()
    _ensure_tools_at_use(ad, w.store, "linux-64")
    assert len(ad.pushed) == 2
    assert w.store.get_site("fake")["tools"]["state"] == "ready"


def test_ready_row_is_presence_only(w):
    """The hot path costs two file_exists round-trips per BUILD and
    nothing else — no --version probes, no pushes."""
    w.store.put_site("fake", "ssh", {})
    w.store.set_site_tools("fake", {"state": "ready", "at": 0})
    ad = FakeAdapter()
    ad._exists = {"bin/pixi", "bin/pixi-unpack"}
    _ensure_tools_at_use(ad, w.store, "linux-64")
    assert ad.pushed == []
    assert [c for c in ad.calls if c[0] == "run_cmd"] == []
    assert len([c for c in ad.calls if c[0] == "file_exists"]) == 2


def test_ready_row_with_vanished_binary_repushes(w, fake_fetch):
    w.store.put_site("fake", "ssh", {})
    w.store.set_site_tools("fake", {"state": "ready", "at": 0})
    ad = FakeAdapter()                          # nothing exists on site
    _ensure_tools_at_use(ad, w.store, "linux-64")
    assert len(ad.pushed) == 2                  # self-healed


def test_unprovisionable_refuses_with_levers(w, monkeypatch):
    """If bin/pixi still isn't real after the ensure, the build refuses
    HERE with the levers — not three minutes later as an unclassified
    `pixi install` log tail."""
    import weft.site_tools as st
    monkeypatch.setattr(st, "fetch_tool", lambda *a: (_ for _ in ()).throw(
        WeftError("site.bootstrap_failed", "no network", stage="infra",
                  retryable=True)))
    w.store.put_site("fake", "ssh", {})
    ad = FakeAdapter()
    with pytest.raises(WeftError) as ei:
        _ensure_tools_at_use(ad, w.store, "linux-64")
    e = ei.value
    assert e.code == "env.realize_failed" and e.retryable
    assert "pixi_source" in e.hints["levers"]
    assert "WEFT_PIXI_VERSION" in e.hints["levers"]["version"]
    row = w.store.get_site("fake")["tools"]
    assert row["state"] == "failed"             # honest row for the card


def test_reconcile_resumes_interrupted_push(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local", {
        "root": str(tmp_path / "site"), "pixi_source": pixi_bin},
        tools="skip")
    # forge the state a mid-push crash leaves behind
    w.store.set_site_tools("local", {"state": "preparing", "at": 0})
    del w
    w2 = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    acts = w2.reconcile()
    assert any(a.get("action") == "resume-tools" for a in acts)
    assert _wait_tools(w2, "local")["state"] == "ready"


def test_push_binary_concurrent_same_dest(tmp_path):
    """Unique tmp + atomic replace: two concurrent pushes of the same
    binary must both succeed with intact content (the fixed '.tmp'
    suffix used to tear and fail both)."""
    from weft.adapters.local import LocalAdapter
    src = tmp_path / "tool"
    src.write_bytes(b"BINARY" * 1000)
    ad = LocalAdapter("l", tmp_path / "root")
    errs = []

    def push():
        try:
            for _ in range(20):
                ad._push_binary(src, "bin/tool")
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=push) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs
    assert (tmp_path / "root" / "bin" / "tool").read_bytes() == \
        src.read_bytes()
    stray = list((tmp_path / "root" / "bin").glob("*.tmp.*"))
    assert not stray, stray


def test_tools_state_vocabulary():
    assert tools_state({"pixi": "ok", "pixi-unpack": "ok"}) == "ready"
    assert tools_state({"pixi": "pushed for linux-64: pixi 0.72",
                        "pixi-unpack": "ok"}) == "ready"
    assert tools_state({"pixi": "ok",
                        "pixi-unpack": "unavailable: x"}) == "partial"
    assert tools_state({"pixi": "pushed but not runnable: exec fmt",
                        "pixi-unpack": "unavailable: x"}) == "failed"
    assert tools_state({}) == "failed"
