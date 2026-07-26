"""PYTHONNOUSERSITE umbrella (aba note ask 3): ~/.local site-packages
must not leak into ANY weft-launched interpreter — the bare node
python (env=None) or a managed env whose version happens to match (a
broken user-site build aborted an import with an opaque SystemError).
Hermetic by default; task env_vars opt out explicitly."""

import pytest

from weft.api import Weft

CHECK = "python3 -c 'import sys; print(sys.flags.no_user_site)'"


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def _run(w, task):
    jid = w.task_submit(task)["job_id"]
    job = w.runner.wait(jid, 120)
    assert job["state"] == "DONE", job.get("error")
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "out.txt")
    return out["preview"]["lines"][0]


def test_env_none_task_is_hermetic(w, monkeypatch):
    """The bare node interpreter — the exact incident shape. The
    controller's own env is scrubbed first: local jobs inherit it, and
    the test suite itself runs under PYTHONNOUSERSITE=1 — without the
    scrub this test would pass by inheritance, not by the umbrella."""
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)
    assert _run(w, {"command": f"{CHECK} > out.txt",
                    "outputs": ["out.txt"], "site": "local"}) == "1"


def test_env_vars_override_wins(w):
    """The opt-out lever: env_vars land after the umbrella, and an
    empty value disables the flag (python treats empty as unset)."""
    assert _run(w, {"command": f"{CHECK} > out.txt",
                    "outputs": ["out.txt"], "site": "local",
                    "env_vars": {"PYTHONNOUSERSITE": ""}}) == "0"


def test_kernel_is_hermetic(w, monkeypatch):
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)
    k = w.kernel_start("local", "python")["kernel_id"]
    try:
        r = w.kernel_exec(
            k, "import sys; print(sys.flags.no_user_site)", timeout=60)
        assert r["rc"] == 0 and r["out"].strip() == "1"
    finally:
        w.kernel_stop(k)


def test_session_exec_is_hermetic(tmp_path, pixi_bin, monkeypatch):
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(
        __file__).parent.parent / "unit"))
    from helpers_verify import cold_session
    w, sid = cold_session(tmp_path, pixi_bin)
    r = w.session_exec(sid, CHECK)
    assert r["rc"] == 0 and r["stdout"].strip() == "1"
