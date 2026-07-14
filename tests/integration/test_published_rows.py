"""Render-complete published rows (weft-ui ask): env_published joins the
catalog's write-time facts with this workspace's read-time truth, so a
host UI is a pure projection — no host-side probes or joins."""

import json

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {
        "root": str(tmp_path / "site"), "pixi_source": pixi_bin,
        "capabilities_override": {"glibc": "2.28"}})
    return w


def _tree_with_catalog(tmp_path):
    tree = tmp_path / "lab-envs"
    tree.mkdir()
    eid_ok = "env:v2:" + "a" * 64
    eid_new = "env:v2:" + "b" * 64
    eid_any = "env:v2:" + "c" * 64
    (tree / "catalog.json").write_text(json.dumps({
        "catalog_version": 1, "envs": {"stack": {
            "latest": "2026.07",
            "versions": {
                "2026.07": {"env_id": eid_ok, "glibc_floor": "2.17",
                            "grade": "fully-pinned",
                            "image_bytes": 5, "notes": ""},
                "2026.08rc": {"env_id": eid_new, "glibc_floor": "2.34",
                              "grade": "fully-pinned",
                              "image_bytes": 5, "notes": ""},
                "pure": {"env_id": eid_any, "glibc_floor": None,
                         "grade": "snapshot-pinned",
                         "image_bytes": 5, "notes": ""},
            }}}}))
    return str(tree), eid_ok, eid_new


def test_rows_carry_read_time_truth(w, tmp_path):
    tree, eid_ok, eid_new = _tree_with_catalog(tmp_path)
    w.store.set_realization(eid_ok, "local", "squashfs",
                            f"{tree}/envs/{'a' * 64}", "ready",
                            read_only=True)
    w.store.touch_realization(eid_ok, "local")
    w.store.set_realization(eid_new, "local", "squashfs", "envs/x",
                            "building")

    out = w.env_published("local", tree=tree)
    assert out["schema"] == "published:v1" and out["site"] == "local"
    vs = out["envs"]["stack"]["versions"]

    ok, rc, pure = vs["2026.07"], vs["2026.08rc"], vs["pure"]
    assert ok["is_latest"] and not rc["is_latest"] and not pure["is_latest"]
    # site glibc 2.28: floor 2.17 runs, floor 2.34 does not, no floor runs
    assert ok["runnable_here"] is True
    assert rc["runnable_here"] is False
    assert pure["runnable_here"] is True
    # realization join: RO adoption vs in-flight build vs nothing
    assert ok["state_here"] == "adopted-ro" and ok["last_used"]
    assert rc["state_here"] == "building"
    assert pure["state_here"] == "missing"
    # write-time facts ride along untouched
    assert ok["grade"] == "fully-pinned"


def test_unknown_site_glibc_stays_unknown(tmp_path, pixi_bin):
    """unknown ≠ runnable: no site glibc means runnable_here=None for
    every row, floor or not."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin,
                                       "capabilities_override": {"glibc": ""}})
    tree, _, _ = _tree_with_catalog(tmp_path)
    vs = w.env_published("local", tree=tree)["envs"]["stack"]["versions"]
    assert all(v["runnable_here"] is None for v in vs.values())
