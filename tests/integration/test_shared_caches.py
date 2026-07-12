"""D1: shared site roots — cross-user build lease, adoption, umask."""

import os
import stat
import threading

import pytest

from weft.api import Weft

TINY = {"name": "shared-tiny", "deps": {"conda": ["xz >=5"]}}


def _mk(tmp_path, pixi_bin, sub, root):
    w = Weft(tmp_path / sub, pixi_bin=pixi_bin)
    w.register_site("shared", "local", {"root": str(root),
                                        "pixi_source": pixi_bin,
                                        "shared": True})
    return w


@pytest.mark.solver
def test_concurrent_builders_share_one_build(tmp_path, pixi_bin):
    """Two 'users' (workspaces) race the same EnvID on a shared root: one
    builds under the lease, the other waits and ADOPTS — never a corrupted
    double build-in-place."""
    root = tmp_path / "shared-root"
    w1 = _mk(tmp_path, pixi_bin, "u1", root)
    w2 = _mk(tmp_path, pixi_bin, "u2", root)
    t = {"command": "xz --version > results/v.txt", "env": None,
         "outputs": ["results/"], "site": "shared"}
    e1 = w1.env_ensure(TINY)["env_id"]
    e2 = w2.env_ensure(TINY)["env_id"]
    assert e1 == e2

    results = {}

    def run(w, key):
        r = w.task_submit({**t, "env": e1})
        results[key] = w.runner.wait(r["job_id"], 900)

    a = threading.Thread(target=run, args=(w1, "u1"))
    b = threading.Thread(target=run, args=(w2, "u2"))
    a.start(); b.start(); a.join(); b.join()
    assert results["u1"]["state"] == "DONE", results["u1"]["error"]
    assert results["u2"]["state"] == "DONE", results["u2"]["error"]
    # exactly one of them adopted (the other built)
    adopted = [
        e for w in (w1, w2)
        for e in w.events_poll(0, 400, compact=False)["events"]
        if e["kind"] == "realize.adopted"
    ]
    assert len(adopted) == 1, adopted
    # no leftover lease
    from weft.realize import env_dir_rel
    assert not (root / (env_dir_rel(e1) + ".lease")).exists()


@pytest.mark.solver
def test_stale_lease_takeover(tmp_path, pixi_bin):
    root = tmp_path / "shared-root"
    w = _mk(tmp_path, pixi_bin, "u1", root)
    env = w.env_ensure(TINY)["env_id"]
    from weft.realize import env_dir_rel
    lease = root / (env_dir_rel(env) + ".lease")
    lease.mkdir(parents=True)
    os.utime(lease, (0, 0))     # ancient: the holder died long ago
    r = w.task_submit({"command": "true", "env": env, "site": "shared"})
    job = w.runner.wait(r["job_id"], 900)
    assert job["state"] == "DONE", job["error"]   # takeover, then build
    assert not lease.exists()


def test_shared_mode_creates_group_writable(tmp_path, pixi_bin):
    root = tmp_path / "shared-root"
    w = _mk(tmp_path, pixi_bin, "u1", root)
    r = w.task_submit({"command": "echo x > results/o.txt",
                       "outputs": ["results/"], "site": "shared"})
    assert w.runner.wait(r["job_id"], 120)["state"] == "DONE"
    cmd = root / "jobs" / r["job_id"] / "cmd.sh"
    assert cmd.exists()
    assert stat.S_IMODE(cmd.stat().st_mode) & stat.S_IWGRP  # group-writable
