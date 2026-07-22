"""Shared harness for verify/ensure tests: a cold session on a laid
fake realization + a marker-routed oracle interceptor."""

import json

from weft.adapters.base import ShimResult
from weft.api import Weft
from weft.realize import env_dir_rel
from weft.verify import MARKER

ENV = "env:v1:deadbeefcafe"


def cold_session(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.store.put_env(ENV, "spec_" + ENV[-12:], {
        "extras": {},
        "platforms": {"any": [{"kind": "pypi", "name": "plotpkg",
                               "version": "1.0"}]},
    }, "lock: {}", "[project]", ["any"])
    rel = env_dir_rel(ENV)
    d = tmp_path / "site" / rel
    (d / "bin").mkdir(parents=True)
    (d / "activate.sh").write_text(f'export PATH="{d}/bin:$PATH"\n')
    (d / ".weft-ready").write_text(json.dumps({"strategy": "prefix"}))
    w.store.set_realization(ENV, "local", "prefix", rel, "ready",
                            read_only=True)
    r = w.session_start(ENV, "local")
    return w, r["session_id"]


def no_toolchain(monkeypatch):
    import weft.toolchain as toolchain
    monkeypatch.setattr(toolchain, "ensure_toolchain", lambda *a, **k: None)


def script_log(monkeypatch, w, answers):
    """Intercept run_cmd/run_activated by substring; returns the log of
    (key, script) matches. Non-matching scripts pass through."""
    ad = w.adapters["local"]
    log = []
    orig_cmd, orig_act = ad.run_cmd, ad.run_activated

    def route(script, orig, timeout):
        for key, resp in answers.items():
            if key in script:
                log.append((key, script))
                return resp() if callable(resp) else resp
        return orig(script, timeout=timeout)

    monkeypatch.setattr(ad, "run_cmd",
                        lambda s, timeout=120.0: route(s, orig_cmd, timeout))
    monkeypatch.setattr(
        ad, "run_activated",
        lambda s, timeout=120.0: route(s, orig_act, timeout))
    return log


def marker(name, ok=True, got=None, kind="metadata"):
    row = {"name": name, "kind": kind, "ok": ok}
    if got:
        row["got"] = got
    return ShimResult(0, MARKER + json.dumps(row), "")
