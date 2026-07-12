"""Item-0 operability levers: queue reasons, realize logs, array retry,
module-system trichotomy — the probes and workarounds an agent reaches for."""

import time

import pytest

from weft.api import Weft


@pytest.fixture
def wl(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def test_array_retry_rejoins_group_and_digests(wl):
    r = wl.task_submit({
        "command": "test \"$WEFT_ARRAY_INDEX\" -eq 1 && exit 5; "
                   "echo ok-$WEFT_ARRAY_INDEX > results/o.txt",
        "outputs": ["results/"], "site": "local", "array": 4})
    group = r["group"]
    for sub in r["jobs"]:
        wl.runner.wait(sub["job_id"], 300)
    st = wl.array_status(group)
    assert st["failed"] == 1 and st["done"] == 3

    ret = wl.array_retry(group, command_override="echo fixed > results/o.txt")
    assert len(ret["retried"]) == 1 and ret["retried"][0]["index"] == 1
    wl.runner.wait(ret["retried"][0]["job_id"], 300)

    st2 = wl.array_status(group)
    assert st2["total"] == 4 and st2["failed"] == 0 and st2["done"] == 4
    # a fresh array.done reflects the healed group
    dones = [e for e in wl.events_poll(0, 800)["events"]
             if e["kind"] == "array.done"]
    assert dones and dones[-1]["failed"] == 0 and dones[-1]["done"] == 4
    # idempotence: nothing left to retry
    assert wl.array_retry(group)["retried"] == []


@pytest.mark.solver
def test_failed_realization_log_exposed(wl):
    env = wl.env_ensure({"name": "bad-hatch", "deps": {"conda": ["xz >=5"]},
                         "post_install": ["no-such-tool --x"]})
    r = wl.task_submit({"command": "true", "env": env["env_id"], "site": "local"})
    job = wl.runner.wait(r["job_id"], 600)
    assert job["state"] == "FAILED"
    st = wl.env_status(env["env_id"])
    failed = [x for x in st["realizations"] if x["state"] == "failed"]
    assert failed and "no-such-tool" in failed[0]["log_tail"]


@pytest.mark.docker
def test_queue_reason_surfaces(tmp_path, pixi_bin, slurm_site):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("hpc", "slurm", {
        "host": slurm_site["host"], "port": slurm_site["port"],
        "user": slurm_site["user"], "ssh_opts": slurm_site["ssh_opts"],
        "root": slurm_site["root"], "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.4
    hog = w.task_submit({"command": "sleep 40", "resources": {"cpus": 8},
                         "site": "hpc"})
    stuck = w.task_submit({"command": "true", "resources": {"cpus": 8},
                           "site": "hpc"})
    # submission threads race: either job may be the one that pends
    reason = None
    for _ in range(120):
        for jid in (stuck["job_id"], hog["job_id"]):
            entry = w.task_status(jid)[0]
            if entry["state"] == "QUEUED" and entry.get("queue_reason"):
                reason = entry["queue_reason"]
        if reason:
            break
        time.sleep(0.5)
    assert reason in ("Resources", "Priority"), reason
    w.task_cancel(hog["job_id"]); w.task_cancel(stuck["job_id"])
    w.runner.wait(hog["job_id"], 60); w.runner.wait(stuck["job_id"], 60)


@pytest.mark.docker
def test_module_trichotomy(tmp_path, pixi_bin, slurm_site):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    # deliberately NO modules_init: MODULEPATH is unset in ssh shells
    w.register_site("hpc", "slurm", {
        "host": slurm_site["host"], "port": slurm_site["port"],
        "user": slurm_site["user"], "ssh_opts": slurm_site["ssh_opts"],
        "root": slurm_site["root"], "pixi_source": pixi_bin})
    chk = w.module_check("hpc", ["espresso/7.2"])
    # module command initializes fine on this fixture; espresso is only
    # findable with MODULEPATH — either verdict must be *explained*
    assert chk["module_system"] in ("ok", "not_initialized")
    if chk["module_system"] == "ok":
        assert chk["modules"]["espresso/7.2"] is False  # MODULEPATH unset
    else:
        assert chk["satisfiable_here"] is False and "modules_init" in chk["note"]
    # local sites: honest "absent"
    w.register_site("l", "local", {"root": str(tmp_path / "s"),
                                   "pixi_source": pixi_bin})
    assert w.module_check("l", ["x"])["module_system"] == "absent"
