"""Regression tests for the live-agent evaluation findings."""

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


@pytest.mark.solver
def test_tampered_env_rebuilds_never_host_fallback(w):
    """Eval finding #1 (correctness): deleting a tool from a realized env
    used to fall through to the host binary with the locked env's name on
    the manifest. Now the executable-inventory fence forces a rebuild."""
    env = w.env_ensure({"name": "fence", "deps": {"conda": ["xz >=5"]}})
    t = {"command": "command -v xz > results/path.txt; "
                    "xz --version >> results/path.txt",
         "env": env["env_id"], "outputs": ["results/"], "site": "local"}
    j1 = w.runner.wait(w.task_submit(t)["job_id"], 900)
    assert j1["state"] == "DONE"
    env_path_line = next(o for o in j1["manifest"]["outputs"]
                         if o["path"] == "results/path.txt"
                         )["preview"]["lines"][0]
    assert "/envs/" in env_path_line  # tool came from the realized env

    # bit-rot / hostile deletion of the tool inside the realization
    from weft.realize import env_dir_rel
    rel = env_dir_rel(env["env_id"])
    w.adapters["local"].run_cmd(
        f"rm -f $WEFT_ROOT/{rel}/.pixi/envs/default/bin/xz")

    j2 = w.runner.wait(w.task_submit(t, force=True)["job_id"], 900)
    assert j2["state"] == "DONE", j2["error"]
    line2 = next(o for o in j2["manifest"]["outputs"]
                 if o["path"] == "results/path.txt")["preview"]["lines"][0]
    assert "/envs/" in line2, "must NEVER silently use the host binary"
    kinds = [e["kind"] for e in w.events_poll(0, 800, compact=False)["events"]]
    assert "realize.integrity_failed" in kinds  # the rebuild was reported


def test_memoized_array_elements_count_in_digests(w):
    """Eval finding #2: memoized elements vanished from group counts."""
    t = {"command": "echo v-$WEFT_ARRAY_INDEX > results/o.txt",
         "outputs": ["results/"], "site": "local", "array": 3}
    r1 = w.task_submit(t)
    for sub in r1["jobs"]:
        w.runner.wait(sub["job_id"], 300)
    assert w.array_status(r1["group"])["done"] == 3

    # resubmit the same array: every element memoizes, digests stay whole
    r2 = w.task_submit(t)
    assert all(j.get("memoized") for j in r2["jobs"])
    st = w.array_status(r2["group"])
    assert st["total"] == 3 and st["done"] == 3, st
    dones = [e for e in w.events_poll(0, 800, compact=False)["events"]
             if e["kind"] == "array.done"
             and e.get("array_group") == r2["group"]]
    assert dones and dones[-1]["done"] == 3