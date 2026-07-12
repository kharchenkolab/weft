"""Agent-in-the-loop evaluation (doc 06 §2, fourth layer).

Each scenario injects a failure; the scripted agent must recover using
only the structured errors — no test peeks at internals to help it. What
these tests actually measure is the *interface*: are the taxonomy codes
and hints good enough that a policy table can navigate them? A regression
here (an unhelpful hint, a wrong code) is an agent-experience bug even
when every unit test passes.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from scripted_agent import ScriptedAgent

from weft.api import Weft


@pytest.fixture
def weft(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def test_recovers_from_oom_by_right_sizing(weft):
    agent = ScriptedAgent(weft)
    # the job's own limit scales with the ask, so a bigger ask really helps
    out = agent.run({
        "command": "ulimit -v $((WEFT_MEM_GB * 1048576)); "
                   "python3 -c 'x = bytearray(1500*1024*1024); print(len(x))' "
                   "> results/alloc.txt",
        "outputs": ["results/"],
        "resources": {"mem_gb": 1},
        "site": "local",
    })
    assert out["success"], out
    assert out["unchanged_resubmits"] == 0
    assert any("raised mem_gb" in a for a in out["actions"]), out["actions"]


def test_recovers_from_walltime_by_extending(weft):
    agent = ScriptedAgent(weft)
    # must outlast limit + the 10s enforcement grace to actually get killed
    out = agent.run({
        "command": "sleep 15; echo done > results/d.txt",
        "outputs": ["results/"],
        "resources": {"walltime": "00:00:02"},
        "site": "local",
    })
    assert out["success"], out
    assert any("walltime" in a for a in out["actions"])
    assert out["unchanged_resubmits"] == 0


def test_recovers_from_capability_violation_by_clamping(weft):
    agent = ScriptedAgent(weft)
    out = agent.run({
        "command": "echo \"cpus=$WEFT_CPUS\" > results/c.txt",
        "outputs": ["results/"],
        "resources": {"cpus": 100000},
        "site": "local",
    })
    assert out["success"], out
    assert any("clamped cpus" in a for a in out["actions"])


@pytest.mark.solver
def test_recovers_from_solve_conflict_by_relaxing_pin(weft):
    agent = ScriptedAgent(weft)
    out = agent.run(
        {"command": "xz --version > results/v.txt",
         "outputs": ["results/"], "site": "local"},
        spec={"name": "over-pinned", "deps": {"conda": ["xz ==4.999.9"]}},
    )
    assert out["success"], out
    assert any("relaxed pin" in a for a in out["actions"])


@pytest.mark.docker
def test_recovers_from_scratch_purge(tmp_path, pixi_bin, sshd_site):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("beamlab", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin,
    })
    data = tmp_path / "ws" / "frames.dat"
    data.write_bytes(b"x" * 300_000)
    ref = w.data_register("frames.dat")["ref"]
    task = {
        "command": "wc -c < data/frames.dat > results/n.txt",
        "inputs": [{"ref": ref, "mount_as": "data/frames.dat"}],
        "outputs": ["results/"], "site": "beamlab",
    }
    agent = ScriptedAgent(w)
    assert agent.run(task)["success"]
    # a site admin purges scratch; a NEW task against the same input must
    # trip the stale location table and recover (an identical resubmission
    # would just memoize — correctly — and never touch the site)
    w.adapters["beamlab"].run_cmd("rm -rf $WEFT_ROOT/cas/*")
    task2 = dict(task, command="sha256sum data/frames.dat | cut -c1-8 > results/h.txt")
    out = agent.run(task2)
    assert out["success"], out
    assert any("retryable" in a for a in out["actions"]), out["actions"]


def test_scorecard(weft):
    """Aggregate recovery-rate metric over the local scenarios (doc 06 §3)."""
    agent = ScriptedAgent(weft)
    scenarios = [
        {"command": "ulimit -v $((WEFT_MEM_GB * 1048576)); "
                    "python3 -c 'x = bytearray(1500*1024*1024)'",
         "resources": {"mem_gb": 1}, "site": "local"},
        {"command": "sleep 5; true", "resources": {"walltime": "00:00:02"},
         "site": "local"},
        {"command": "true", "resources": {"cpus": 99999}, "site": "local"},
    ]
    results = [agent.run(s) for s in scenarios]
    recovered = sum(1 for r in results if r["success"])
    wasted = sum(r["unchanged_resubmits"] for r in results)
    assert recovered == len(scenarios), results
    assert wasted == 0
