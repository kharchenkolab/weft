"""A2: URL ingest into the data plane — controller-side and site-direct."""

import hashlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from weft.api import Weft


@pytest.fixture(scope="module")
def http_source(tmp_path_factory):
    """A local 'remote repository': python http.server over a temp dir."""
    d = tmp_path_factory.mktemp("pub")
    payload = os.urandom(300_000)
    (d / "run2189.dat").write_bytes(payload)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port),
         "--bind", "127.0.0.1", "--directory", str(d)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    yield {"url": f"http://127.0.0.1:{port}/run2189.dat",
           "sha256": hashlib.sha256(payload).hexdigest(),
           "bytes": len(payload)}
    proc.terminate()


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def test_url_ingest_to_workspace(w, http_source):
    r = w.data_register(http_source["url"])
    assert r["ref"] == f"dref:{http_source['sha256']}"
    assert r["bytes"] == http_source["bytes"]
    assert r["trust"] == "first-fetch"
    d = w.data_describe(r["ref"])
    assert d["meta"]["origin"] == http_source["url"]
    # usable as a task input like any local registration
    t = w.task_submit({
        "command": "wc -c < d/in > results/n.txt",
        "inputs": [{"ref": r["ref"], "mount_as": "d/in"}],
        "outputs": ["results/"], "site": "local"})
    job = w.runner.wait(t["job_id"], 120)
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/n.txt")
    assert out["preview"]["lines"] == [str(http_source["bytes"])]


def test_url_ingest_site_direct(w, http_source):
    r = w.data_register(http_source["url"], site="local",
                        expected_sha256=http_source["sha256"])
    assert r["fetched_to"] == "local" and r["trust"] == "verified"
    assert r["ref"] == f"dref:{http_source['sha256']}"
    # present at the site, NOT in the workspace CAS (no detour)
    assert r["ref"] in w.store.refs_present_at("local")
    assert w.cas.kind_of(r["ref"]) is None
    # a task there stages 0 bytes
    t = w.task_submit({
        "command": "sha256sum d/in | cut -d' ' -f1 > results/h.txt",
        "inputs": [{"ref": r["ref"], "mount_as": "d/in"}],
        "outputs": ["results/"], "site": "local"})
    assert t["plan"]["staging"]["bytes_to_move"] == 0
    job = w.runner.wait(t["job_id"], 120)
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/h.txt")
    assert out["preview"]["lines"] == [http_source["sha256"]]


def test_expected_mismatch_and_unknown_scheme(w, http_source):
    bad = w.data_register(http_source["url"], expected_sha256="0" * 64)
    assert bad["error"] == "data.verify_failed"
    assert bad["hints"]["got"] == http_source["sha256"]
    unk = w.data_register("ftp://old.example/x")
    assert unk["error"] == "task.invalid"
    assert "https" in unk["hints"]["registered"]
