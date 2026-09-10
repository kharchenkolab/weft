"""Env-churn round (aba2 asks 36-38): a seconds-shaped pypi layer add
must not silently become minutes of full-prefix churn.

The incident: a pip-less base sent every pypi overlay through
overlay_fallback into a fresh 70-package prefix (~70s cold first-import
each, three times), the fallback's WHY lived only in a site log, an
ensure targeted at a running frozen-env kernel answered satisfied:true
that the kernel could not use, and the dominant cost (223s inside
blocks) was invisible to telemetry."""

import json
import threading
import time

from helpers_verify import ENV, cold_session
from weft import realize
from weft.adapters.base import ShimResult
from weft.errors import WeftError

PIPLESS = "/usr/bin/python: No module named pip\n"


def _rows():
    child = {"canonical": {"platforms": {"linux-64": [
        {"kind": "pypi", "name": "statpack", "version": "1.0",
         "sha256": "a" * 64}]}, "extras": {}}}
    parent = {"canonical": {"platforms": {"linux-64": []}, "extras": {},
                            "layers": {}}}
    return child, parent


def _drive_overlay(w, monkeypatch, result):
    ad = w.adapters["local"]
    seen = []
    orig = ad.run_activated

    def route(script, timeout=120.0):
        if "-overlay-pypi.log" in script:
            seen.append(script)
            return result
        return orig(script, timeout=timeout)

    monkeypatch.setattr(ad, "run_activated", route)
    child, parent = _rows()
    (w.workspace / "x").mkdir(exist_ok=True) if hasattr(w, "workspace") \
        else None
    return seen, lambda: realize._overlay_pypi(
        "env:v1:child0001", child, parent, ad, "envs/child0001",
        ad.path("envs/deadbeefcafe"), ["statpack"], "")


# ── ask 36.1: uv-first, pip fallback (lane parity) ─────────────────────────

def test_overlay_pypi_tries_uv_before_pip(tmp_path, pixi_bin,
                                          monkeypatch):
    w, _sid = cold_session(tmp_path, pixi_bin)
    seen, drive = _drive_overlay(
        w, monkeypatch, ShimResult(0, "#overlay uv", ""))
    line = drive()
    assert "PYTHONPATH" in line
    s = seen[0]
    assert "command -v uv" in s and "uv pip install" in s, \
        "the overlay lane carries the session lanes' uv arm (parity)"
    assert s.index("command -v uv") < s.index("python -m pip"), \
        "uv first — it needs no pip in the parent"
    assert "--python" in s, "uv targets the PARENT's interpreter"
    assert "#overlay uv" in s and "#overlay pip" in s, \
        "the marker says which installer actually ran"


# ── ask 36.2: pip-missing is a CLASSIFIED cause ────────────────────────────

def test_pip_missing_classifier_families():
    from weft.evidence import _pip_missing_hints
    h = _pip_missing_hints(PIPLESS)
    assert h["failure_class"] == "pip_missing"
    assert "deps.conda" in h["remedy"] and "uv" in h["remedy"], \
        "the remedy names the levers each surface can pull"
    assert _pip_missing_hints("No module named pipdeptree") is None, \
        "word boundary — pip, not pip-prefixed names"
    assert _pip_missing_hints("") is None


def test_overlay_failure_carries_pip_missing_class(tmp_path, pixi_bin,
                                                   monkeypatch):
    w, _sid = cold_session(tmp_path, pixi_bin)
    _seen, drive = _drive_overlay(
        w, monkeypatch, ShimResult(1, PIPLESS, ""))
    try:
        drive()
        raise AssertionError("should have raised")
    except WeftError as e:
        assert e.hints["failure_class"] == "pip_missing"
        assert "log_path" in e.hints, "the full log still travels"


def test_fallback_event_names_the_cause(tmp_path, pixi_bin,
                                        monkeypatch):
    """The incident's WHY lived only in a site log — the fallback
    event now carries the classified cause alongside the reason."""
    w, _sid = cold_session(tmp_path, pixi_bin)
    plat = realize._site_platform((w.store.get_site("local") or {})
                                  .get("capabilities") or {})
    env2 = "env:v1:churn0001"
    w.store.put_env(env2, "spec_churn0001", {
        "extras": {},
        "platforms": {plat: [{"kind": "pypi", "name": "statpack",
                              "version": "1.0"}]},
    }, "lock: {}", "[workspace]", [plat])
    monkeypatch.setattr(realize, "_overlay_parent",
                        lambda *a, **k: (ENV, None))
    monkeypatch.setattr(
        realize, "_build_overlay",
        lambda *a, **k: (_ for _ in ()).throw(WeftError(
            "env.realize_failed", "could not install the pypi delta",
            stage="realize",
            hints={"failure_class": "pip_missing"})))
    for name in ("_build_prefix", "_realize_layers",
                 "_stage_post_install_inputs", "_run_post_install",
                 "_install_url_pins", "_spot_check_and_mark"):
        monkeypatch.setattr(realize, name, lambda *a, **k: None)
    realize.ensure_realization(
        env2, w.store.get_env(env2), w.adapters["local"], w.store,
        caps=(w.store.get_site("local") or {}).get("capabilities"))
    evs = [e for e in w.store.events_since(0, 300)
           if e["kind"] == "realize.overlay_fallback"]
    assert evs, "the fallback must be narrated"
    assert evs[0]["failure_class"] == "pip_missing", \
        "the CAUSE, not just the wrapper text"
    assert "reason" in evs[0]


def test_session_pypi_failure_carries_pip_missing(tmp_path, pixi_bin,
                                                  monkeypatch):
    """Lane parity: the session fetch lane classifies the same cause."""
    from helpers_verify import script_log
    import weft.toolchain as tc
    w, sid = cold_session(tmp_path, pixi_bin)
    monkeypatch.setattr(tc, "ensure_toolchain", lambda *a, **k: None)
    s = w.store.get_session(sid)
    report = (tmp_path / "site" / s["location"] / "pip-report.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(
        {"install": [{"metadata": {"name": "statpack",
                                   "version": "1.0"}}]}))
    script_log(monkeypatch, w, {
        "--dry-run": ShimResult(0, "", ""),
        "#fetch uv": ShimResult(1, PIPLESS, ""),
    })
    out = w.session_install(sid, pypi=["statpack"])
    assert out["error"] == "env.realize_failed"
    assert out["hints"]["failure_class"] == "pip_missing"


# ── ask 38: wall_ms + prefix.done log_path ─────────────────────────────────

def test_block_results_carry_wall_ms(tmp_path, pixi_bin):
    w, _sid = cold_session(tmp_path, pixi_bin)
    k = w.kernel_start("local", "python")["kernel_id"]
    try:
        ok = w.kernel_exec(k, "import time; time.sleep(0.3)",
                           wait=True, timeout=20)
        assert ok["rc"] == 0
        assert 250 <= ok["wall_ms"] < 20000, ok.get("wall_ms")
        bad = w.kernel_exec(k, "raise RuntimeError('x')",
                            wait=True, timeout=20)
        assert bad["rc"] != 0 and "wall_ms" in bad
        evs = [e for e in w.store.events_since(0, 300)
               if e["kind"] == "kernel.block_failed"]
        assert evs and isinstance(evs[-1]["wall_ms"], int), \
            "the failure event carries the in-block cost"
    finally:
        w.kernel_stop(k)


def test_all_three_drivers_stamp_wall_before_rc():
    from pathlib import Path
    d = Path(realize.__file__).parent / "kernels"
    for name in ("driver.py", "driver.R", "driver.jl"):
        src = (d / name).read_text()
        assert "wall_ms" in src, f"{name} lost the timing stamp"
        assert src.index("wall_ms") < src.rindex("rc_f"), \
            f"{name}: wall must land BEFORE rc (rc is the consume signal)"


def test_prefix_done_carries_log_path(tmp_path, pixi_bin, monkeypatch):
    w, _sid = cold_session(tmp_path, pixi_bin)
    ad = w.adapters["local"]
    orig = ad.run_cmd

    def route(script, timeout=120.0):
        if "install --frozen" in script:
            return ShimResult(0, "", "")
        if "shell-hook" in script:
            return ShimResult(0, 'export PATH="/x:$PATH"\n', "")
        return orig(script, timeout=timeout)

    monkeypatch.setattr(ad, "run_cmd", route)
    events = []
    realize._build_prefix(
        "env:v1:aa", {"manifest": "[workspace]", "native_lock": "l",
                      "canonical": {"platforms": {"linux-64": []}}},
        ad, "envs/aa", [],
        emit=lambda kind, **kw: events.append((kind, kw)))
    done = [kw for kind, kw in events if kind == "realize.prefix.done"]
    assert done and done[0]["log_path"].endswith("-prefix.log"), \
        "success points at the persisted pixi output (attribution " \
        "without parsing numbers weft cannot vouch for)"
