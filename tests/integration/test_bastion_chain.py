"""Round B: multi-hop reachability — the dominant alien-cluster topology
(target has internet OUT but is reachable only through a bastion), plus
the hop diagnostics and tunnel self-healing that make it dependable."""

import subprocess
import time

import pytest

from weft.api import Weft

pytestmark = pytest.mark.docker


@pytest.fixture
def w(tmp_path, pixi_bin, bastion_chain):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("inner", "ssh", {
        "host": bastion_chain["host"], "user": bastion_chain["user"],
        "jump": bastion_chain["jump"],
        "ssh_opts": bastion_chain["ssh_opts"],
        "root": bastion_chain["root"], "pixi_source": pixi_bin,
    })
    return w


def test_full_flow_through_the_bastion(w):
    """Register → probe → run a task with outputs, all through the hop."""
    caps = w.sites_describe("inner")["capabilities"]
    assert caps["schema"] == "capabilities:v2"
    j = w.runner.wait(w.task_submit({
        "command": "hostname > results/h.txt",
        "outputs": ["results/"], "site": "inner"})["job_id"], 600)
    assert j["state"] == "DONE", j["error"]
    out = next(o for o in j["manifest"]["outputs"]
               if o["path"] == "results/h.txt")
    assert out["preview"]["lines"]           # ran on the inner box


def test_hop_check_walks_the_chain(w, bastion_chain):
    hops = w.adapters["inner"].hop_check()
    assert all(h["ok"] for h in hops), hops
    assert len(hops) == 2                    # bastion, then target
    assert bastion_chain["jump"][0] in hops[0]["hop"]


def test_doctor_names_the_dead_hop(w, bastion_chain):
    """Pause the bastion: doctor must say the chain breaks THERE, not just
    'site unreachable'."""
    subprocess.run(["docker", "pause", bastion_chain["bastion"]],
                   capture_output=True, check=True)
    try:
        w.adapters["inner"].connect_timeout = 5
        doc = w.doctor()
        entry = next(c for c in doc["sites"] if c["site"] == "inner")
        assert entry["ok"] is False
        assert entry["hops"][0]["ok"] is False       # the bastion
        assert "chain breaks at" in entry["diagnosis"]
        assert bastion_chain["jump"][0] in entry["diagnosis"]
    finally:
        subprocess.run(["docker", "unpause", bastion_chain["bastion"]],
                       capture_output=True)


def test_chain_heals_after_bastion_restart(w, bastion_chain):
    """Kill the ControlMaster's world: restart the bastion mid-session.
    The next command must transparently re-establish (ControlMaster=auto),
    not wedge on the stale socket."""
    j = w.runner.wait(w.task_submit({"command": "true", "site": "inner"},
                                    force=True)["job_id"], 300)
    assert j["state"] == "DONE"
    subprocess.run(["docker", "restart", "-t", "1",
                    bastion_chain["bastion"]], capture_output=True,
                   check=True)
    deadline = time.time() + 60
    last = None
    while time.time() < deadline:
        r = w.task_submit({"command": "echo healed", "site": "inner"},
                          force=True)
        if "job_id" in r:
            last = w.runner.wait(r["job_id"], 300)
            if last["state"] == "DONE":
                break
        time.sleep(2)
    assert last and last["state"] == "DONE", last


def test_service_tunnel_self_heals(w, bastion_chain, linux_platforms):
    """A service endpoint through the chain survives a tunnel drop: the
    next status() re-establishes and says so. (The env realizes through
    the hop too — the target has internet OUT, just no reachability IN.)"""
    env = w.env_ensure({"name": "srv", "platforms": linux_platforms,
                        "deps": {"conda": ["python =3.12"]}})["env_id"]
    r = w.service_start("inner", {
        "command": "python -m http.server $WEFT_PORT --bind 127.0.0.1",
        "env": env,
    }, ports=[18474], ready_timeout=120)
    assert "service_id" in r, r
    sid = r["service_id"]
    try:
        import urllib.request
        url = r["endpoints"][0]["url"]
        assert urllib.request.urlopen(url, timeout=10).status == 200

        # a REAL drop: restart the bastion — the mux master (which owns
        # the -L forward) dies with its TCP link, taking the tunnel along.
        # (pkill of the -f client is NOT a drop: the forward lives in the
        # master and survives — multiplexing resilience, tested above.)
        subprocess.run(["docker", "restart", "-t", "1",
                        bastion_chain["bastion"]], capture_output=True,
                       check=True)
        deadline = time.time() + 90
        healed = False
        while time.time() < deadline:
            st = w.service_status(sid)
            if "endpoints" in st:
                try:
                    if urllib.request.urlopen(
                            st["endpoints"][0]["url"], timeout=10
                            ).status == 200:
                        healed = True
                        break
                except OSError:
                    pass
            time.sleep(3)
        assert healed, "tunnel never healed after the bastion restart"
        events = [e["kind"] for e in
                  w.events_poll(0, 900, compact=False)["events"]]
        assert "service.tunnel_lost" in events
        assert "service.tunnel_restored" in events
    finally:
        w.service_stop(sid)
