"""Round I, docker half: direct-pull between two REAL machines (no shared
volume) — dst pulls from src with its own key over the container network;
peer_host models 'the address peers use differs from the controller's'."""

import subprocess
import time
import uuid

import pytest

from weft.api import Weft

pytestmark = pytest.mark.docker

REPO_SSH_OPTS = ["-o", "StrictHostKeyChecking=no",
                 "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "IdentitiesOnly=yes"]


@pytest.fixture
def pair(docker_available, tmp_path_factory):
    """Two sshd containers on one private network; the second holds the
    key so IT can reach the first (the user's own trust, pre-existing)."""
    keydir = tmp_path_factory.mktemp("routekeys")
    build = subprocess.run(
        ["sh", "tests/fixtures/sshd/build.sh", str(keydir)],
        capture_output=True, text=True)
    if build.returncode != 0:
        pytest.skip(f"cannot build sshd fixture: {build.stderr[-200:]}")
    net = f"weft-rnet-{uuid.uuid4().hex[:8]}"
    a = f"weft-rsrc-{uuid.uuid4().hex[:8]}"
    b = f"weft-rdst-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "network", "create", net], capture_output=True)
    for name in (a, b):
        r = subprocess.run(["docker", "run", "-d", "--rm", "--name", name,
                            "--network", net, "-p", "127.0.0.1::22",
                            "weft-test-sshd"], capture_output=True)
        if r.returncode != 0:
            subprocess.run(["docker", "rm", "-f", a, b], capture_output=True)
            subprocess.run(["docker", "network", "rm", net],
                           capture_output=True)
            pytest.skip("cannot start route containers")

    def port(name):
        out = subprocess.run(["docker", "port", name, "22"],
                             capture_output=True, text=True).stdout
        return int(out.strip().rsplit(":", 1)[-1])

    key = str(keydir / "id_ed25519")
    # B gets the private key: B→A trust exists BEFORE weft looks at it
    subprocess.run(["docker", "cp", key, f"{b}:/home/physicist/.ssh/id_ed25519"])
    subprocess.run(["docker", "exec", b, "sh", "-c",
                    "chown physicist:physicist /home/physicist/.ssh/id_ed25519"
                    " && chmod 600 /home/physicist/.ssh/id_ed25519"
                    " && printf 'Host *\\n  StrictHostKeyChecking no\\n' "
                    " > /home/physicist/.ssh/config"
                    " && chown physicist:physicist /home/physicist/.ssh/config"])
    opts = ["-i", key, *REPO_SSH_OPTS]
    for name in (a, b):
        for _ in range(60):
            if subprocess.run(["ssh", *opts, "-o", "BatchMode=yes",
                               "-p", str(port(name)),
                               "physicist@127.0.0.1", "true"],
                              capture_output=True).returncode == 0:
                break
            time.sleep(0.5)
    yield {"a": a, "b": b, "a_port": port(a), "b_port": port(b),
           "ssh_opts": opts}
    subprocess.run(["docker", "rm", "-f", a, b], capture_output=True)
    subprocess.run(["docker", "network", "rm", net], capture_output=True)


def test_direct_pull_between_machines(pair, tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("srcbox", "ssh", {
        "host": "127.0.0.1", "port": pair["a_port"], "user": "physicist",
        "ssh_opts": pair["ssh_opts"], "root": "/home/physicist/.weft",
        "pixi_source": pixi_bin,
        # how PEERS reach this box (container network), vs how the
        # controller does (mapped port on localhost)
        "peer_host": pair["a"], "peer_port": 22,
    })
    w.register_site("dstbox", "ssh", {
        "host": "127.0.0.1", "port": pair["b_port"], "user": "physicist",
        "ssh_opts": pair["ssh_opts"], "root": "/home/physicist/.weft",
        "pixi_source": pixi_bin,
    })
    route = w.store.get_route("srcbox", "dstbox")
    assert route and route["direct_ssh"], route
    assert route["src_addr"].startswith(f"physicist@{pair['a']}")
    assert not route["shared_fs_path"]

    # produce ~3MB at src (bare task: fixture has no python)
    j = w.runner.wait(w.task_submit({
        "command": "head -c 3000000 /dev/urandom > results/run.bin",
        "outputs": ["results/"], "site": "srcbox"})["job_id"], 900)
    assert j["state"] == "DONE", j["error"]
    ref = next(o["ref"] for o in j["manifest"]["outputs"]
               if o["path"] == "results/run.bin")
    assert w.cas.kind_of(ref) is None

    task = {"command": "wc -c < d/run.bin > results/n.txt",
            "inputs": [{"ref": ref, "mount_as": "d/run.bin"}],
            "outputs": ["results/"], "site": "dstbox"}
    plan = w.task_submit(task, dry_run=True)
    assert plan["plan"]["staging"]["site_to_site"][0]["via"] == "direct-pull"
    j2 = w.runner.wait(w.task_submit(task, force=True)["job_id"], 900)
    assert j2["state"] == "DONE", j2["error"]
    out = next(o for o in j2["manifest"]["outputs"]
               if o["path"] == "results/n.txt")
    assert "3000000" in out["preview"]["lines"][0]

    # bytes never touched the controller
    assert w.cas.kind_of(ref) is None
    events = [e for e in w.events_poll(0, 900, compact=False)["events"]
              if e["kind"] == "transfer.done"]
    assert any(e.get("via") == "direct-pull" and e.get("src") == "srcbox"
               for e in events)
