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
