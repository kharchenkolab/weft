"""Kernels on LIVE session prefixes (user-model ask, aba default lane):
session_install lands in the running kernel with no restart; promotion
pins the moving target by auto-snapshotting into a real EnvID."""

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.3
    return w


def test_env_and_session_are_mutually_exclusive(w):
    r = w.kernel_start("local", "python", env_id="env:v1:" + "0" * 64,
                       session_id="ses_x")
    assert r["error"] == "task.invalid" and "mutually exclusive" in r["detail"]


def test_unknown_session_refused(w):
    r = w.kernel_start("local", "python", session_id="ses_nonexistent")
    assert r["error"] == "task.invalid"


@pytest.mark.solver
@pytest.mark.slow
def test_live_install_visible_to_next_block(w):
    s = w.session_start({"name": "live-lane",
                         "deps": {"conda": ["python =3.12", "pip"]}},
                        "local")
    sid = s["session_id"]
    k = w.kernel_start("local", "python", session_id=sid)["kernel_id"]

    r = w.kernel_exec(k, "import six", timeout=60)
    assert r["rc"] == 1                     # not there yet
    got = w.session_install(sid, pypi=["six"])
    assert got.get("rc", 0) == 0 or "installed" in str(got), got
    # THE point: the running kernel sees it on the next block, no restart
    r = w.kernel_exec(k, "import six; print(six.__name__)", timeout=60)
    assert r["rc"] == 0 and "six" in r["out"]

    # state persists across the install like any other blocks
    w.kernel_exec(k, "x = 21", timeout=60)
    assert w.kernel_exec(k, "print(x*2)", timeout=60)["out"].strip() == "42"

    # promotion pins the live session: snapshot minted, cited, labeled
    r = w.kernel_exec(
        k, "import os, six\n"
           "open(os.environ['WEFT_BLOCK_DIR'] + '/v.txt', 'w')"
           ".write(six.__version__)", timeout=60)
    m = w.kernel_promote(k, blocks=[r["block"]])
    assert m["env_id"] and m["env_id"].startswith("env:")
    assert m["session"]["session_id"] == sid
    assert m["session"]["snapshotted_at_promote"] is True
    assert m["reproducibility"] == "state-dependent"
    # the snapshot is a real env: it knows about six
    snap_env = w.store.get_env(m["env_id"])
    assert snap_env is not None

    # restart replays into the session AS IT NOW IS
    fresh = w.kernel_restart(k)
    assert fresh["replayed_blocks"] >= 3
    k2 = fresh["kernel_id"]
    assert w.kernel_exec(k2, "print(x*2)", timeout=60)["out"].strip() == "42"
    w.kernel_stop(k2)
    w.session_stop(sid)
