"""bug5 A1, observed lane — the FULL-PREFIX realize path summons
weft's toolchain on a compile-signature failure.

The incident's own evidence was env.realize_failed@realize ("pixi
install failed on local"): pixi install builds pypi sdists through uv
with whatever PATH holds, and this lane never provisioned a compiler
— consumers worked around it by shipping c-compiler/cxx-compiler in
deps.conda, the exact shape toolchain.py's header rejects. One retry,
gated on the compile signature; solve/network deaths keep the single
attempt and its evidence."""

import json

from helpers_verify import cold_session
from weft.adapters.base import ShimResult
from weft.errors import WeftError
from weft.realize import _build_prefix

GXX = ("building 'annoy.annoylib' extension\n"
       "error: [Errno 2] No such file or directory: 'g++'\n")
NET = "failed to fetch: https://conda.anaconda.org/... timed out\n"
TC_MARK = "/fake/tc/.pixi/envs/default/bin"

ENV_ROW = {"manifest": "[workspace]", "native_lock": "lock: {}",
           "canonical": {"platforms": {"linux-64": []}}}


def _rig(tmp_path, pixi_bin, monkeypatch, first_tail,
         second=ShimResult(0, "", ""), toolchain="/fake/tc"):
    w, _sid = cold_session(tmp_path, pixi_bin)
    import weft.toolchain as tc
    monkeypatch.setattr(tc, "ensure_toolchain",
                        lambda *a, **k: toolchain)
    ad = w.adapters["local"]
    installs, orig = [], ad.run_cmd

    def route(script, timeout=120.0):
        if "install --frozen" in script:
            installs.append(script)
            return (ShimResult(1, first_tail, "")
                    if len(installs) == 1 else second)
        if "shell-hook" in script:
            return ShimResult(0, 'export PATH="/x:$PATH"\n', "")
        return orig(script, timeout=timeout)

    monkeypatch.setattr(ad, "run_cmd", route)
    events = []
    return w, ad, installs, lambda kind, **kw: events.append(kind), events


def test_compile_death_retries_with_prelude(tmp_path, pixi_bin,
                                            monkeypatch):
    w, ad, installs, emit, events = _rig(tmp_path, pixi_bin,
                                         monkeypatch, GXX)
    _build_prefix("env:v1:aa", ENV_ROW, ad, "envs/aa", [], emit=emit,
                  site_platform="linux-64")
    assert len(installs) == 2, "exactly one retry"
    assert TC_MARK in installs[1] and TC_MARK not in installs[0]
    assert "-prefix-tc.log" in installs[1], \
        "the retry persists its own log — the first attempt's survives"
    assert "realize.toolchain_retry" in events
    assert "realize.prefix.done" in events


def test_network_death_never_retries(tmp_path, pixi_bin, monkeypatch):
    w, ad, installs, emit, events = _rig(tmp_path, pixi_bin,
                                         monkeypatch, NET)
    try:
        _build_prefix("env:v1:aa", ENV_ROW, ad, "envs/aa", [],
                      emit=emit, site_platform="linux-64")
        raise AssertionError("should have raised")
    except WeftError as e:
        assert e.code == "env.realize_failed"
        assert "log_path" in e.hints
    assert len(installs) == 1
    assert "realize.toolchain_retry" not in events


def test_retry_failure_evidence_points_at_retry_log(tmp_path, pixi_bin,
                                                    monkeypatch):
    w, ad, installs, emit, events = _rig(
        tmp_path, pixi_bin, monkeypatch, GXX,
        second=ShimResult(1, "fatal error: zlib.h: No such file or "
                             "directory\ncompilation terminated.", ""))
    try:
        _build_prefix("env:v1:aa", ENV_ROW, ad, "envs/aa", [],
                      emit=emit, site_platform="linux-64")
        raise AssertionError("should have raised")
    except WeftError as e:
        assert len(installs) == 2
        assert "-prefix-tc.log" in e.hints["log_path"], \
            "evidence names the log of the attempt that DECIDED"
        # after the toolchain retry, the residual failure IS the
        # missing-header class — this lane classifies it like every
        # sibling (parity sweep gap 3)
        assert e.hints.get("failure_class") == "missing_system_lib"
        assert "deps.conda" in e.hints.get("remedy", ""), \
            "the remedy names the lever THIS surface can pull"


def test_unprovisionable_toolchain_keeps_original_error(
        tmp_path, pixi_bin, monkeypatch):
    w, ad, installs, emit, events = _rig(tmp_path, pixi_bin,
                                         monkeypatch, GXX,
                                         toolchain=None)
    try:
        _build_prefix("env:v1:aa", ENV_ROW, ad, "envs/aa", [],
                      emit=emit, site_platform="linux-64")
        raise AssertionError("should have raised")
    except WeftError as e:
        assert e.code == "env.realize_failed"
    assert len(installs) == 1


def test_realize_public_path_arms_the_retry(tmp_path, pixi_bin,
                                            monkeypatch):
    """The arm is a keyword the CALLER threads (site_platform) — this
    drives the public env_realize verb to prove the production path
    arms it, so a future caller passing nothing is caught here, not
    in the field."""
    from weft.api import Weft
    from weft.realize import _site_platform
    w = Weft(tmp_path / "ws2", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site2"),
                                       "pixi_source": pixi_bin})
    plat = _site_platform((w.store.get_site("local") or {})
                          .get("capabilities") or {})
    w.store.put_env("env:v1:feedbead0001", "spec_feedbead0001", {
        "extras": {},
        "platforms": {plat: [{"kind": "pypi", "name": "statpack",
                              "version": "1.0"}]},
    }, "lock: {}", "[workspace]", [plat])
    import weft.toolchain as tc
    seen = {}

    def fake_ensure(adapter, pixi_bin_, platform, extra_deps=(),
                    *, emit=None):
        seen["platform"] = platform
        return None      # provisioning declines; original error stands

    monkeypatch.setattr(tc, "ensure_toolchain", fake_ensure)
    ad = w.adapters["local"]
    orig = ad.run_cmd

    def route(script, timeout=120.0):
        if "install --frozen" in script:
            return ShimResult(1, GXX, "")
        return orig(script, timeout=timeout)

    monkeypatch.setattr(ad, "run_cmd", route)
    out = w.env_realize("env:v1:feedbead0001", "local")
    assert out.get("error") == "env.realize_failed"
    assert seen.get("platform"), \
        "the public realize path must thread site_platform into the arm"
