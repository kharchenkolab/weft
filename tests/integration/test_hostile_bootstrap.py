"""Bootstrap on machines you actually get handed: old glibc HPC nodes,
minimal images with no python/curl/rsync, and musl boxes where glibc
environments are impossible and must fail with a *cause*, not a mystery.
"""

import subprocess
import time
import uuid
from pathlib import Path

import pytest

from weft.api import Weft

pytestmark = [pytest.mark.docker, pytest.mark.slow]

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def hostile_keys(tmp_path_factory):
    keydir = tmp_path_factory.mktemp("hostilekeys")
    r = subprocess.run(
        ["sh", str(REPO_ROOT / "tests/fixtures/hostile/build.sh"), str(keydir)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"cannot build hostile fixtures: {r.stderr[-400:]}")
    return keydir


def _boot(image: str, keydir) -> dict:
    name = f"weft-hostile-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name,
         "-p", "127.0.0.1::22", image],
        capture_output=True, text=True,
    )
    assert run.returncode == 0, run.stderr
    port = subprocess.run(["docker", "port", name, "22"],
                          capture_output=True, text=True
                          ).stdout.strip().rsplit(":", 1)[-1]
    opts = ["-i", str(keydir / "id_ed25519"), "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "IdentitiesOnly=yes"]
    for _ in range(60):
        ok = subprocess.run(
            ["ssh", *opts, "-o", "BatchMode=yes", "-p", port,
             "physicist@127.0.0.1", "echo up"], capture_output=True)
        if ok.returncode == 0:
            break
        time.sleep(0.5)
    else:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        pytest.skip(f"{image} sshd never came up")
    return {"container": name, "port": int(port), "ssh_opts": opts}


def _weft_for(tmp_path, pixi_bin, box, site_name) -> Weft:
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site(site_name, "ssh", {
        "host": "127.0.0.1", "port": box["port"], "user": "physicist",
        "ssh_opts": box["ssh_opts"], "root": "/home/physicist/.weft",
        "pixi_source": pixi_bin,
    })
    return w


@pytest.mark.solver
def test_rocky8_old_glibc_full_path(tmp_path, pixi_bin, hostile_keys,
                                    linux_platforms):
    """RHEL8-era node (glibc 2.28): bootstrap, realize, run — the common
    'old OS on the cluster' case must just work."""
    box = _boot("weft-test-rocky8", hostile_keys)
    try:
        w = _weft_for(tmp_path, pixi_bin, box, "rocky")
        caps = w.sites_describe("rocky")["capabilities"]
        assert caps["glibc"].startswith("2.28")
        env = w.env_ensure({"name": "r8", "platforms": linux_platforms,
                            "deps": {"conda": ["xz >=5"]}})
        r = w.task_submit({"command": "xz --version > results/v.txt",
                           "env": env["env_id"], "outputs": ["results/"],
                           "site": "rocky"})
        assert "job_id" in r, r
        job = w.runner.wait(r["job_id"], 600)
        assert job["state"] == "DONE", job["error"]
    finally:
        subprocess.run(["docker", "rm", "-f", box["container"]],
                       capture_output=True)


def test_bare_debian_no_tools(tmp_path, pixi_bin, hostile_keys):
    """No curl, no wget, no rsync, no python on the box. Bootstrap must
    succeed, the probe must degrade honestly, and staging must fall back
    to the tar-over-ssh pipe."""
    box = _boot("weft-test-bare", hostile_keys)
    try:
        w = _weft_for(tmp_path, pixi_bin, box, "bare")
        caps = w.sites_describe("bare")["capabilities"]
        # no curl/wget: the probe cannot prove internet, so it must say False
        # (conservative: strategy will choose packed delivery)
        assert caps["internet"] is False
        assert caps["runtimes"]["rsync"] is False

        data = tmp_path / "ws" / "run.dat"
        data.write_bytes(b"detector-frame\n" * 4096)
        ref = w.data_register("run.dat")["ref"]
        endpoint = w.adapters["bare"].transfer_endpoint()
        assert endpoint["method"] == "ssh-pipe"

        r = w.task_submit({
            "command": "wc -c < data/run.dat > results/size.txt",
            "inputs": [{"ref": ref, "mount_as": "data/run.dat"}],
            "outputs": ["results/"], "site": "bare",
        })
        assert "job_id" in r, r
        job = w.runner.wait(r["job_id"], 300)
        assert job["state"] == "DONE", job["error"]
        out = next(o for o in job["manifest"]["outputs"]
                   if o["path"] == "results/size.txt")
        assert out["preview"]["lines"] == [str(15 * 4096)]

        # and bulk fetch back over the same pipe
        got = w.data_fetch(out["ref"], "back/size.txt")
        assert Path(got["path"]).read_text().strip() == str(15 * 4096)
    finally:
        subprocess.run(["docker", "rm", "-f", box["container"]],
                       capture_output=True)


@pytest.mark.solver
def test_bare_debian_packed_env(tmp_path, pixi_bin, hostile_keys,
                                linux_platforms):
    """internet=False on the box → strategy picks packed; the archive rides
    ssh-pipe; the env works with zero site-side network."""
    box = _boot("weft-test-bare", hostile_keys)
    try:
        w = _weft_for(tmp_path, pixi_bin, box, "bare2")
        env = w.env_ensure({"name": "bare-packed", "platforms": linux_platforms,
                            "deps": {"conda": ["xz >=5"]}})
        r = w.task_submit({"command": "xz --version > results/v.txt",
                           "env": env["env_id"], "outputs": ["results/"],
                           "site": "bare2"})
        assert "job_id" in r, r
        job = w.runner.wait(r["job_id"], 900)
        assert job["state"] == "DONE", job["error"]
        real = w.store.get_realization(env["env_id"], "bare2")
        assert real["strategy"] == "packed"
    finally:
        subprocess.run(["docker", "rm", "-f", box["container"]],
                       capture_output=True)


@pytest.mark.solver
def test_musl_box_fails_with_cause_but_runs_bare_tasks(tmp_path, pixi_bin,
                                                       hostile_keys,
                                                       linux_platforms):
    """Alpine/musl: realized envs are impossible — the agent must get
    env.unsatisfiable_on_site naming musl, not a cryptic loader error.
    Env-less tasks still work (busybox userland is enough for the shim)."""
    box = _boot("weft-test-musl", hostile_keys)
    try:
        w = _weft_for(tmp_path, pixi_bin, box, "musl")
        caps = w.sites_describe("musl")["capabilities"]
        assert caps["glibc"] == "musl"

        # bare tasks: fine — the shim is POSIX sh + busybox
        r = w.task_submit({"command": "uname -a > results/u.txt",
                           "outputs": ["results/"], "site": "musl"})
        assert "job_id" in r, r
        job = w.runner.wait(r["job_id"], 300)
        assert job["state"] == "DONE", job["error"]

        # env tasks: structured refusal with remediation
        env = w.env_ensure({"name": "nope", "platforms": linux_platforms,
                            "deps": {"conda": ["xz >=5"]}})
        r2 = w.task_submit({"command": "xz --version", "env": env["env_id"],
                            "site": "musl"})
        job2 = w.runner.wait(r2["job_id"], 300)
        assert job2["state"] == "FAILED"
        err = job2["error"]
        assert err["error"] == "env.unsatisfiable_on_site"
        assert "musl" in err["detail"]
        assert "glibc site" in err["hints"]["suggestion"]
    finally:
        subprocess.run(["docker", "rm", "-f", box["container"]],
                       capture_output=True)
