"""Phase 3 acceptance (doc 06): cloud burst with hard budget caps.

The mock provisioner launches real Docker containers — provision →
bootstrap → run → teardown is exercised end to end; only the money and
the cloud API are simulated. Acceptance: a launch over the cap is refused
before anything is provisioned, and the runaway watchdog fires during a
simulated overrun, tearing the instance down.
"""

import subprocess
import time
import uuid

import pytest

from weft.api import Weft

pytestmark = pytest.mark.docker


class DockerProvisioner:
    """Stands in for SkyPilot: 'instances' are containers."""

    def __init__(self, keydir, rate: float):
        self.keydir = keydir
        self.rate = rate
        self.launched: list[str] = []

    def launch(self) -> str:
        name = f"weft-cloud-{uuid.uuid4().hex[:8]}"
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", name,
             "-p", "127.0.0.1::22", "weft-test-sshd"],
            check=True, capture_output=True,
        )
        self.launched.append(name)
        port = subprocess.run(["docker", "port", name, "22"],
                              capture_output=True, text=True
                              ).stdout.strip().rsplit(":", 1)[-1]
        self._port = int(port)
        for _ in range(60):
            ok = subprocess.run(
                ["ssh", *self._opts(), "-o", "BatchMode=yes", "-p", str(port),
                 "physicist@127.0.0.1", "true"], capture_output=True)
            if ok.returncode == 0:
                break
            time.sleep(0.5)
        return name

    def _opts(self):
        return ["-i", str(self.keydir / "id_ed25519"),
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null", "-o", "IdentitiesOnly=yes"]

    def describe(self, handle: str) -> dict:
        return {"host": "127.0.0.1", "port": self._port, "user": "physicist",
                "ssh_opts": self._opts(), "root": "/home/physicist/.weft-cloud"}

    def teardown(self, handle: str) -> None:
        subprocess.run(["docker", "rm", "-f", handle], capture_output=True)

    def rate_usd_per_hour(self) -> float:
        return self.rate

    def alive(self, handle: str) -> bool:
        r = subprocess.run(["docker", "inspect", handle], capture_output=True)
        return r.returncode == 0


@pytest.fixture
def cloud_weft(tmp_path, pixi_bin, sshd_site, tmp_path_factory):
    """Weft with a mock cloud provisioner registered (keys reuse the sshd
    fixture image, which is already built with them)."""
    from pathlib import Path
    # sshd_site's ssh_opts carry "-i <keyfile>"; the same key opens the
    # containers our provisioner launches from the same image
    keydir = Path(sshd_site["ssh_opts"][1]).parent

    def make(w: Weft, rate: float, budget: dict):
        prov = DockerProvisioner(keydir, rate)
        w.provisioners["mock"] = lambda cfg: prov
        w.register_site("cloud-a100", "cloud", {
            "provisioner": "mock",
            "budget": budget,
            "resources": {"cpus": 8, "mem_gb": 32},
            "pixi_source": w.pixi_bin,
        })
        return prov

    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.make_cloud = lambda rate, budget: make(w, rate, budget)
    return w


def test_burst_run_and_teardown(cloud_weft, tmp_path):
    prov = cloud_weft.make_cloud(rate=1.0, budget={"max_usd": 10, "max_hours": 1})
    # registration alone must not launch anything (spending is explicit)
    assert prov.launched == []

    r = cloud_weft.task_submit({
        "command": "echo burst-done > results/out.txt",
        "outputs": ["results/"], "site": "cloud-a100",
    })
    assert "job_id" in r, r
    job = cloud_weft.runner.wait(r["job_id"], 300)
    assert job["state"] == "DONE", job["error"]
    assert len(prov.launched) == 1 and prov.alive(prov.launched[0])

    # results were collected; now the user tears the instance down
    out = cloud_weft.site_teardown("cloud-a100")
    assert out["state"] == "terminated"
    assert not prov.alive(prov.launched[0])
    kinds = [e["kind"] for e in cloud_weft.events_poll(0, 500)["events"]]
    assert "cloud.launched" in kinds and "cloud.teardown" in kinds


def test_over_cap_launch_refused_before_provisioning(cloud_weft):
    prov = cloud_weft.make_cloud(rate=32.0, budget={"max_usd": 5, "max_hours": 1})
    r = cloud_weft.task_submit({"command": "true", "site": "cloud-a100"})
    job = cloud_weft.runner.wait(r["job_id"], 60)
    assert job["state"] == "FAILED"
    err = job["error"]
    assert err["error"] == "budget.exceeded"
    assert "refused" in err["detail"]
    assert err["hints"]["cap_usd"] == 5
    assert prov.launched == []  # nothing was provisioned, nothing to pay for


def test_runaway_watchdog_fires_and_terminates(cloud_weft):
    # $3600/h = $1/s; cap $3 → the watchdog must fire ~3s after launch,
    # mid-job, cancel it, and terminate the instance
    prov = cloud_weft.make_cloud(
        rate=3600.0, budget={"max_usd": 3.0, "max_hours": 0.0005})
    cloud_weft.runner.poll_interval = 0.5
    r = cloud_weft.task_submit({"command": "sleep 120", "site": "cloud-a100"})
    assert "job_id" in r, r
    job = cloud_weft.runner.wait(r["job_id"], 120)
    assert job["state"] == "FAILED"
    err = job["error"]
    assert err["error"] == "budget.exceeded"
    assert "watchdog" in err["detail"]
    assert err["hints"]["instance"] == "terminated"
    assert prov.launched and not prov.alive(prov.launched[0])
    kinds = [e["kind"] for e in cloud_weft.events_poll(0, 500)["events"]]
    assert "budget.watchdog" in kinds
