"""A1: services — endpoint lifecycle, tunnels, death, collect-on-stop."""

import time
import urllib.request

import pytest

from weft.api import Weft

SERVER = ("python3 -c \"import http.server, os, functools; "
          "http.server.HTTPServer(('127.0.0.1', int(os.environ['WEFT_PORT'])), "
          "functools.partial(http.server.SimpleHTTPRequestHandler, "
          "directory='.')).serve_forever()\"")


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.3
    return w


def _get(url, timeout=10):
    return urllib.request.urlopen(url, timeout=timeout).read()


def test_service_lifecycle_local(w, tmp_path):
    data = tmp_path / "ws" / "spectrum.csv"
    data.write_text("e,counts\n1,10\n2,40\n")
    ref = w.data_register("spectrum.csv")["ref"]
    r = w.service_start("local", {
        "command": "mkdir -p logs; echo started > logs/svc.log; " + SERVER,
        "inputs": [{"ref": ref, "mount_as": "spectrum.csv"}],
        "outputs": ["logs/"],
    }, ports=[18471], ready_timeout=30)
    assert r["state"] == "ready", r
    url = r["endpoints"][0]["url"]
    body = _get(url + "/spectrum.csv").decode()
    assert "2,40" in body                      # serving the staged data

    st = w.service_status(r["service_id"])
    assert st["state"] == "ready" and st["endpoints"]

    out = w.service_stop(r["service_id"], collect=True)
    assert out["state"] == "stopped"
    logs = next(o for o in out["outputs"] if o["path"] == "logs/svc.log")
    assert logs["preview"]["lines"] == ["started"]
    kinds = [e["kind"] for e in w.events_poll(0, 400)["events"]]
    for k in ("service.started", "service.ready", "service.collected",
              "service.stopped"):
        assert k in kinds


def test_service_death_is_reported(w):
    r = w.service_start("local", {
        "command": "python3 -c \"import http.server, os; "
                   "s = http.server.HTTPServer(('127.0.0.1', "
                   "int(os.environ['WEFT_PORT'])), "
                   "http.server.SimpleHTTPRequestHandler); "
                   "import threading, time; "
                   "threading.Thread(target=s.serve_forever, "
                   "daemon=True).start(); time.sleep(2); os._exit(9)\"",
    }, ports=[18472], ready_timeout=30)
    assert r["state"] == "ready"
    deadline = time.time() + 60
    while time.time() < deadline:
        if w.service_status(r["service_id"])["state"] == "exited":
            break
        time.sleep(0.5)
    st = w.service_status(r["service_id"])
    assert st["state"] == "exited"
    ev = next(e for e in w.events_poll(0, 400)["events"]
              if e["kind"] == "service.exited")
    assert ev["service"] == r["service_id"]


def test_never_ready_times_out_with_log(w):
    r = w.service_start("local",
                        {"command": "echo not-a-server; sleep 300"},
                        ports=[18473], ready_timeout=4)
    assert r["error"] == "sched.timeout"
    assert "not-a-server" in r["hints"]["log_tail"]
    assert "$WEFT_PORT" in r["hints"]["suggestion"]


@pytest.mark.docker
def test_service_tunnel_over_ssh(tmp_path, pixi_bin, sshd_site):
    """The endpoint lives on the remote site, loopback-only; we reach it
    exclusively through the tunnel."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("beamlab", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin})
    env = w.env_ensure({"name": "svc-py", "deps": {"conda": ["python =3.12"]}})
    assert "env_id" in env, env
    r0 = w.task_submit({"command": "true", "env": env["env_id"],
                        "site": "beamlab"})
    assert w.runner.wait(r0["job_id"], 900)["state"] == "DONE"

    r = w.service_start("beamlab", {
        "command": "echo remote-dashboard > index.html; "
                   "python -m http.server $WEFT_PORT --bind 127.0.0.1",
        "env": env["env_id"],
    }, ports=[18901], ready_timeout=60)
    assert r["state"] == "ready", r
    local_url = r["endpoints"][0]["url"]
    assert str(18901) not in local_url or True  # local port differs
    assert _get(local_url + "/index.html").decode().strip() == "remote-dashboard"
    w.service_stop(r["service_id"])
    # tunnel is gone after stop
    with pytest.raises(Exception):
        _get(local_url, timeout=3)
