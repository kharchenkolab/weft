"""#57: directory-as-a-unit registration on a site. A .zarr-style
store already on a site becomes a tree ref without moving — hashed
site-side, blobs hardlinked into the site CAS, fetchable home as a
unit, with run lineage when it lives under a retained tree. Identity
is content: the convention matches output collection exactly."""

import json
from pathlib import Path

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def _run(w, cmd):
    r = w.task_submit({"command": cmd, "site": "local"})
    assert w.runner.wait(r["job_id"], 120)["state"] == "DONE"
    return r["job_id"]


ZARR = ("mkdir -p out/store.zarr/g1 && "
        "printf '{}' > out/store.zarr/.zattrs && "
        "printf chunk0 > out/store.zarr/g1/0.0")


def test_directory_registers_and_fetches_home_as_a_unit(w, tmp_path):
    jid = _run(w, ZARR)
    a = w.adapters["local"]
    abs_path = a.path(f"jobs/{jid}/out/store.zarr")
    r = w.data_register(abs_path, site="local")
    assert r["kind"] == "tree" and r["files"] == 2
    assert r["bytes"] == len("{}") + len("chunk0")
    # blobs are HARDLINKS into the site CAS — the original didn't move
    chunk = Path(abs_path) / "g1" / "0.0"
    manifest = w.cas.tree_manifest(r["ref"])
    sha = next(e["sha256"] for e in manifest if e["path"] == "g1/0.0")
    blob = Path(a.path(f"cas/{sha[:2]}/{sha}"))
    assert blob.stat().st_ino == chunk.stat().st_ino
    # identical content re-registered mints the identical ref
    assert w.data_register(abs_path, site="local")["ref"] == r["ref"]
    # ... and the unit comes home as a directory, byte for byte
    back = tmp_path / "back"
    w.data_fetch(r["ref"], str(back))
    assert (back / ".zattrs").read_text() == "{}"
    assert (back / "g1" / "0.0").read_text() == "chunk0"


def test_evolved_store_reuses_old_blobs(w, tmp_path):
    """The content-addressing dividend: appending a chunk re-registers
    as a NEW tree that shares every old blob — placement and any later
    fetch touch only the new bytes."""
    jid = _run(w, ZARR)
    a = w.adapters["local"]
    abs_path = a.path(f"jobs/{jid}/out/store.zarr")
    v1 = w.data_register(abs_path, site="local")
    w.data_fetch(v1["ref"], str(tmp_path / "v1"))

    (Path(abs_path) / "g1" / "0.1").write_text("chunk1")   # the append
    v2 = w.data_register(abs_path, site="local")
    assert v2["ref"] != v1["ref"] and v2["files"] == 3
    m1 = {e["sha256"] for e in w.cas.tree_manifest(v1["ref"])
          if e["kind"] == "file"}
    m2 = {e["sha256"] for e in w.cas.tree_manifest(v2["ref"])
          if e["kind"] == "file"}
    assert m1 < m2 and len(m2 - m1) == 1                   # shared blobs
    # the v1 fetch already brought the shared blobs home: only the new
    # chunk is absent before the v2 fetch, present after
    new_sha = next(iter(m2 - m1))
    assert not (w.cas.root / new_sha[:2] / new_sha).exists()
    w.data_fetch(v2["ref"], str(tmp_path / "v2"))
    assert (w.cas.root / new_sha[:2] / new_sha).exists()
    assert (tmp_path / "v2" / "g1" / "0.1").read_text() == "chunk1"


def test_retained_directory_carries_run_lineage(tmp_path, pixi_bin):
    """The re-entry doctrine for directory artifacts: a retained store
    registered for new compute walks provenance THROUGH the producing
    run."""
    keep = tmp_path / "longterm"
    keep.mkdir()
    w = Weft(tmp_path / "ws2", pixi_bin=pixi_bin)
    w.register_site("local", "local", {
        "root": str(tmp_path / "site2"), "pixi_source": pixi_bin,
        "retain": {"dir": str(keep)}})
    w.runner.poll_interval = 0.2
    jid = _run(w, ZARR)
    kept = w.run_retain(jid, include=["out/store.zarr"],
                        background=False)
    assert kept["state"] == "done" and kept["in_place"] is True
    reg = w.data_register(f"{kept['location']['path']}/out/store.zarr",
                          site="local")
    assert reg["kind"] == "tree"
    row = w.store.get_dataref(reg["ref"])
    assert row["meta"]["origin"] == f"run:{jid}/out/store.zarr"
    prov = w.provenance(reg["ref"])
    assert jid in json.dumps(prov)


def test_empty_directory_is_refused_honestly(w):
    jid = _run(w, "mkdir -p out/empty")
    r = w.data_register(w.adapters["local"].path(f"jobs/{jid}/out/empty"),
                        site="local")
    assert r["error"] == "data.missing" and "no files" in r["detail"]


def test_missing_path_still_honest(w):
    r = w.data_register("/nowhere/at/all", site="local")
    assert r["error"] == "data.missing"


@pytest.mark.docker
def test_remote_directory_fetches_home_over_the_wire(tmp_path, pixi_bin,
                                                     sshd_site):
    """The perm-storage scenario end to end: a directory on a REMOTE
    site registers without moving, then rebuilds at the workspace as a
    unit — blobs crossing the wire hash-verified."""
    w = Weft(tmp_path / "ws-r", pixi_bin=pixi_bin)
    w.register_site("beam", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.3
    r = w.task_submit({"command": ZARR, "site": "beam"})
    jid = r["job_id"]
    assert w.runner.wait(jid, 300)["state"] == "DONE"
    a = w.adapters["beam"]
    reg = w.data_register(a.path(f"jobs/{jid}/out/store.zarr"),
                          site="beam")
    assert reg["kind"] == "tree" and reg["files"] == 2
    assert reg["fetched_to"] == "beam"          # bytes did NOT move yet
    back = tmp_path / "back"
    w.data_fetch(reg["ref"], str(back))
    assert (back / ".zattrs").read_text() == "{}"
    assert (back / "g1" / "0.0").read_text() == "chunk0"
