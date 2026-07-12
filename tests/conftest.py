import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _sh(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


@pytest.fixture(scope="session")
def pixi_bin() -> str:
    p = REPO_ROOT / ".env" / "bin" / "pixi"
    if p.exists():
        return str(p)
    found = shutil.which("pixi")
    if not found:
        pytest.skip("pixi binary not available")
    return found


@pytest.fixture(scope="session")
def docker_available() -> bool:
    if os.system("docker info >/dev/null 2>&1") != 0:
        pytest.skip("docker not available")
    return True


@pytest.fixture(scope="session")
def sshd_site(docker_available, tmp_path_factory):
    """A dockerized 'remote workstation': sshd + rsync + glibc userland.

    Yields the config dict for Weft.register_site(kind='ssh').
    """
    keydir = tmp_path_factory.mktemp("sshkeys")
    build = _sh("sh", str(REPO_ROOT / "tests/fixtures/sshd/build.sh"), str(keydir))
    if build.returncode != 0:
        pytest.skip(f"cannot build sshd fixture: {build.stderr[-300:]}")
    name = f"weft-sshd-{uuid.uuid4().hex[:8]}"
    run = _sh("docker", "run", "-d", "--rm", "--name", name,
              "-p", "127.0.0.1::22", "weft-test-sshd")
    assert run.returncode == 0, run.stderr
    cid = run.stdout.strip()
    port = _sh("docker", "port", name, "22").stdout.strip().rsplit(":", 1)[-1]
    key = str(keydir / "id_ed25519")
    ssh_opts = [
        "-i", key, "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null", "-o", "IdentitiesOnly=yes",
    ]
    for _ in range(60):
        ok = _sh("ssh", *ssh_opts, "-o", "BatchMode=yes", "-p", port,
                 "physicist@127.0.0.1", "echo ready")
        if ok.returncode == 0:
            break
        time.sleep(0.5)
    else:
        _sh("docker", "rm", "-f", name)
        pytest.skip("sshd fixture container never became reachable")
    yield {
        "container": name, "cid": cid,
        "host": "127.0.0.1", "port": int(port), "user": "physicist",
        "ssh_opts": ssh_opts, "root": "/home/physicist/.weft",
    }
    _sh("docker", "rm", "-f", name)


@pytest.fixture(scope="session")
def bastion_chain(docker_available, tmp_path_factory):
    """The dominant alien-cluster topology: a TARGET reachable only
    through a BASTION (target publishes no ports; both live on a private
    docker network). Yields config for a `jump:`-chained ssh site."""
    keydir = tmp_path_factory.mktemp("bastionkeys")
    build = _sh("sh", str(REPO_ROOT / "tests/fixtures/sshd/build.sh"), str(keydir))
    if build.returncode != 0:
        pytest.skip(f"cannot build sshd fixture: {build.stderr[-300:]}")
    net = f"weft-bnet-{uuid.uuid4().hex[:8]}"
    assert _sh("docker", "network", "create", net).returncode == 0
    bastion = f"weft-bastion-{uuid.uuid4().hex[:8]}"
    target = f"weft-target-{uuid.uuid4().hex[:8]}"
    # EXPLICIT host port: docker re-allocates ephemeral (`::22`) mappings
    # on restart, which no real bastion does — the chaos tests restart
    # the bastion and expect it back at the same address
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        fixed_port = s.getsockname()[1]
    rb = _sh("docker", "run", "-d", "--rm", "--name", bastion,
             "--network", net, "-p", f"127.0.0.1:{fixed_port}:22",
             "weft-test-sshd")
    rt = _sh("docker", "run", "-d", "--rm", "--name", target,
             "--network", net, "weft-test-sshd")    # NO published ports
    if rb.returncode != 0 or rt.returncode != 0:
        _sh("docker", "rm", "-f", bastion, target)
        _sh("docker", "network", "rm", net)
        pytest.skip("cannot start bastion chain containers")
    port = str(fixed_port)
    key = str(keydir / "id_ed25519")
    ssh_opts = [
        "-i", key, "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null", "-o", "IdentitiesOnly=yes",
    ]
    # -J would NOT carry our key/host-key opts to the hop; explicit
    # ProxyCommand does (same mechanism the adapter uses)
    import shlex as _shlex
    proxy = " ".join(_shlex.quote(c) for c in [
        "ssh", "-o", "BatchMode=yes", *ssh_opts, "-p", port,
        "-W", "%h:%p", "physicist@127.0.0.1"])
    for _ in range(60):
        ok = _sh("ssh", *ssh_opts, "-o", "BatchMode=yes",
                 "-o", f"ProxyCommand={proxy}",
                 f"physicist@{target}", "echo ready")
        if ok.returncode == 0:
            break
        time.sleep(0.5)
    else:
        _sh("docker", "rm", "-f", bastion, target)
        _sh("docker", "network", "rm", net)
        pytest.skip("bastion chain never became reachable")
    yield {
        "bastion": bastion, "target": target, "network": net,
        "host": target, "user": "physicist",
        "jump": [f"physicist@127.0.0.1:{port}"],
        "ssh_opts": ssh_opts, "root": "/home/physicist/.weft",
        "bastion_port": int(port),
    }
    _sh("docker", "rm", "-f", bastion, target)
    _sh("docker", "network", "rm", net)


@pytest.fixture(scope="session")
def slurm_site(docker_available, tmp_path_factory):
    """A dockerized single-node Slurm cluster reachable over SSH."""
    keydir = tmp_path_factory.mktemp("slurmkeys")
    build = _sh("sh", str(REPO_ROOT / "tests/fixtures/slurm/build.sh"), str(keydir))
    if build.returncode != 0:
        pytest.skip(f"cannot build slurm fixture: {build.stderr[-300:]}")
    name = f"weft-slurm-{uuid.uuid4().hex[:8]}"
    run = _sh("docker", "run", "-d", "--rm", "--name", name,
              "--hostname", "weftslurm", "-p", "127.0.0.1::22", "weft-test-slurm")
    assert run.returncode == 0, run.stderr
    port = _sh("docker", "port", name, "22").stdout.strip().rsplit(":", 1)[-1]
    key = str(keydir / "id_ed25519")
    ssh_opts = [
        "-i", key, "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null", "-o", "IdentitiesOnly=yes",
    ]
    ready = False
    for _ in range(120):
        ok = _sh("ssh", *ssh_opts, "-o", "BatchMode=yes", "-p", port,
                 "physicist@127.0.0.1",
                 "sinfo -h -o %a 2>/dev/null | head -1")
        if ok.returncode == 0 and "up" in ok.stdout.lower():
            ready = True
            break
        time.sleep(0.5)
    if not ready:
        logs = _sh("docker", "logs", name).stderr[-500:]
        _sh("docker", "rm", "-f", name)
        pytest.skip(f"slurm fixture never became ready: {logs}")
    yield {
        "container": name,
        "host": "127.0.0.1", "port": int(port), "user": "physicist",
        "ssh_opts": ssh_opts, "root": "/home/physicist/.weft",
        "modules_init": "export MODULEPATH=/opt/site-modules",
    }
    _sh("docker", "rm", "-f", name)
