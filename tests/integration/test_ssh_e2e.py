"""Scenario S1 over a dockerized SSH workstation: bootstrap, offload,
cache-hit second run, transfers with verification."""

import time

import pytest

from weft.api import Weft

pytestmark = pytest.mark.docker


@pytest.fixture
def weft_ssh(tmp_path, pixi_bin, sshd_site):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("beamlab", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin,
    })
    return w


def test_bootstrap_and_probe(weft_ssh):
    sites = weft_ssh.sites_list()
    beamlab = next(s for s in sites if s["name"] == "beamlab")
    assert beamlab["scheduler"] == "none"
    caps = weft_ssh.sites_describe("beamlab")["capabilities"]
    assert caps["os"] == "linux" and caps["arch"] == "x86_64"
    assert caps["internet"] is True  # docker bridge has NAT
    # bootstrap is idempotent and cheap the second time
    t0 = time.time()
    weft_ssh.adapters["beamlab"].ensure_bootstrap()
    assert time.time() - t0 < 5


def test_offload_with_staging_and_chaining(weft_ssh, tmp_path):
    data = tmp_path / "ws" / "bpm.csv"
    data.write_text("turn,x_mm\n" + "\n".join(f"{i},{(i % 7) - 3}" for i in range(5000)))
    ref = weft_ssh.data_register("bpm.csv")["ref"]

    r = weft_ssh.task_submit({
        "command": "awk -F, 'NR>1 {s+=$2; n+=1} END {printf \"%.4f\\n\", s/n}' "
                   "data/bpm.csv > results/mean.txt",
        "inputs": [{"ref": ref, "mount_as": "data/bpm.csv"}],
        "outputs": ["results/"],
        "site": "beamlab",
    })
    assert "job_id" in r, r
    assert r["plan"]["staging"]["bytes_to_move"] > 0
    job = weft_ssh.runner.wait(r["job_id"], 120)
    assert job["state"] == "DONE", job["error"]
    mean = next(o for o in job["manifest"]["outputs"]
                if o["path"] == "results/mean.txt")
    assert mean["preview"]["lines"] == ["-0.0010"]

    # second submission with same input: 0 bytes to move (dedup by hash)
    r2 = weft_ssh.task_submit({
        "command": "wc -l < data/bpm.csv > results/n.txt",
        "inputs": [{"ref": ref, "mount_as": "data/bpm.csv"}],
        "outputs": ["results/"],
        "site": "beamlab",
    })
    assert r2["plan"]["staging"]["bytes_to_move"] == 0
    job2 = weft_ssh.runner.wait(r2["job_id"], 120)
    assert job2["state"] == "DONE", job2["error"]

    # chain: output consumed remotely without leaving the site
    out_ref = mean["ref"]
    r3 = weft_ssh.task_submit({
        "command": "cat inputs/mean.txt > results/copy.txt",
        "inputs": [{"ref": out_ref, "mount_as": "inputs/mean.txt"}],
        "outputs": ["results/"],
        "site": "beamlab",
    })
    assert r3["plan"]["staging"]["bytes_to_move"] == 0
    job3 = weft_ssh.runner.wait(r3["job_id"], 120)
    assert job3["state"] == "DONE", job3["error"]


def test_fetch_output_back(weft_ssh):
    r = weft_ssh.task_submit({
        "command": "seq 1 100 > results/series.dat",
        "outputs": ["results/"], "site": "beamlab",
    })
    job = weft_ssh.runner.wait(r["job_id"], 120)
    assert job["state"] == "DONE", job["error"]
    ref = next(o["ref"] for o in job["manifest"]["outputs"]
               if o["path"] == "results/series.dat")
    got = weft_ssh.data_fetch(ref, "retrieved/series.dat")
    from pathlib import Path
    assert Path(got["path"]).read_text().splitlines()[-1] == "100"


@pytest.mark.solver
def test_remote_prefix_realization(weft_ssh):
    """A tiny real env realized over SSH inside the container."""
    ensured = weft_ssh.env_ensure({"name": "tiny-remote",
                                   "deps": {"conda": ["xz >=5"]}})
    assert "env_id" in ensured, ensured
    r = weft_ssh.task_submit({
        "command": "xz --version > results/version.txt",
        "env": ensured["env_id"],
        "outputs": ["results/"],
        "site": "beamlab",
    })
    assert "job_id" in r, r
    job = weft_ssh.runner.wait(r["job_id"], 600)
    assert job["state"] == "DONE", job["error"]
    v = next(o for o in job["manifest"]["outputs"]
             if o["path"] == "results/version.txt")
    assert "xz" in v["preview"]["lines"][0].lower()
    # realization is recorded and second task plans a cache hit
    st = weft_ssh.env_status(ensured["env_id"])
    assert any(re["site"] == "beamlab" and re["state"] == "ready"
               for re in st["realizations"])
