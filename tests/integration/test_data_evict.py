"""data_evict (footprint round 26): the targeted reclaim verb for
staged data copies. ONE evaluator serves dry_run and execution
(conformance-pinned); refusals are typed per gate; force covers
weft-owned copies only (never external homes)."""

import base64

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def _staged_ref(w, tmp_path, data=b"evictable-bytes"):
    """A ref with TWO copies: workspace CAS + staged at the site (via a
    task that mounts it)."""
    p = tmp_path / "in.bin"
    p.write_bytes(data)
    ref = w.data_register(str(p))["ref"]
    jid = w.task_submit({"command": "wc -c < data/in.bin > n.txt",
                         "inputs": [{"ref": ref, "mount_as":
                                     "data/in.bin"}],
                         "outputs": ["n.txt"], "site": "local"})["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    return ref


def test_evict_site_copy_with_receipt_and_event(w, tmp_path):
    ref = _staged_ref(w, tmp_path)
    locs_before = {l["site"] for l in w.store.locations_of(ref)}
    assert "local" in locs_before and "@workspace" in locs_before
    got = w.data_evict(ref, at="local")
    assert got["bytes_freed"] == 15
    assert "@workspace" in got["remaining"]
    assert "local" not in {l["site"] for l in w.store.locations_of(ref)}
    kinds = [e["kind"] for e in w.store.events_since(0, 500)]
    assert "data.evicted" in kinds
    # reversible: the next task re-stages from the workspace copy
    jid = w.task_submit({"command": "wc -c < data/in.bin > n.txt",
                         "inputs": [{"ref": ref, "mount_as":
                                     "data/in.bin"}],
                         "outputs": ["n.txt"], "site": "local"})["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"


def test_last_copy_refuses_then_force_destroys(w, tmp_path):
    p = tmp_path / "only.bin"
    p.write_bytes(b"solo")
    ref = w.data_register(str(p))["ref"]      # ONLY copy: workspace CAS
    out = w.data_evict(ref, at="@workspace")
    assert out["error"] == "data.last_copy"
    assert "copies_checked" in out["hints"]
    assert w.cas._blob_path(ref[5:]).exists()          # nothing deleted
    forced = w.data_evict(ref, at="@workspace", force=True)
    assert "error" not in forced, forced
    assert not w.cas._blob_path(ref[5:]).exists()


def test_pinned_workspace_refusal_is_typed(w, tmp_path):
    ref = _staged_ref(w, tmp_path)            # job input => pinned
    out = w.data_evict(ref, at="@workspace")
    assert out["error"] == "data.pinned"
    assert "provenance" in out["detail"]
    forced = w.data_evict(ref, at="@workspace", force=True)
    assert "error" not in forced and forced["bytes_freed"] > 0


def test_external_home_refuses_even_forced(w, tmp_path):
    d = tmp_path / "site" / "perm" / "store"
    d.mkdir(parents=True)
    (d / "big.bin").write_bytes(b"x" * 64)
    ref = w.data_register(str(d), site="local", ingest=False)["ref"]
    for kwargs in ({}, {"force": True}):      # force does NOT override
        out = w.data_evict(ref, at="local", **kwargs)
        assert out["error"] == "data.external_home", out
        assert "home" in out["hints"]
    assert (d / "big.bin").exists()           # the owner's files: intact


def test_dry_run_is_the_same_evaluator(w, tmp_path):
    """The conformance pin: dry_run's receipt and the real receipt
    agree field-for-field on the same state; a dry_run refusal embeds
    instead of raising."""
    ref = _staged_ref(w, tmp_path)
    dry = w.data_evict(ref, at="local", dry_run=True)
    assert dry["would_free_bytes"] == 15
    assert "error" not in dry and "refusal" not in dry
    real = w.data_evict(ref, at="local")
    assert real["bytes_freed"] == dry["would_free_bytes"]
    assert real["remaining"] == dry["remaining"]
    # refusal embeds in dry-run (the confirm sheet renders it)
    p = tmp_path / "solo2.bin"
    p.write_bytes(b"zz")
    solo = w.data_register(str(p))["ref"]
    dry2 = w.data_evict(solo, at="@workspace", dry_run=True)
    assert dry2["refusal"]["error"] == "data.last_copy"
    assert dry2["would_free_bytes"] == 0
    assert w.cas._blob_path(solo[5:]).exists()         # nothing happened


def test_tree_partial_eviction_keeps_last_copy_members(w, tmp_path):
    """Per-member partition: an ingested tree at the site whose members
    partly exist elsewhere — evict what is safe, keep what is not, say
    both."""
    d = tmp_path / "site" / "perm" / "tree.zarr"
    (d / "c").mkdir(parents=True)
    (d / "c" / "0").write_bytes(b"chunk-A" * 10)
    (d / "c" / "1").write_bytes(b"chunk-B" * 10)
    ref = w.data_register(str(d), site="local", ingest=True)["ref"]
    # fetch ONE member's bytes home so it has a second copy
    import json
    m = next(e for e in w.cas.tree_manifest(ref) if e["path"] == "c/0")
    w.data_fetch(f"dref:{m['sha256']}", str(tmp_path / "out.bin"))
    got = w.data_evict(ref, at="local")
    assert f"dref:{m['sha256']}" in got.get("evicted_members", []), got
    kept_refs = {k["ref"] for k in got.get("kept", [])}
    assert kept_refs, got                      # c/1 is last-copy: kept
    assert all(k["why"] == "last_copy" for k in got["kept"])


def test_evict_intake_refusals(w, tmp_path):
    out = w.data_evict("dref:" + "0" * 64, at="local")
    assert out["error"] == "data.missing"
    p = tmp_path / "z.bin"
    p.write_bytes(b"z")
    ref = w.data_register(str(p))["ref"]
    out = w.data_evict(ref, at="nonexistent-site")
    assert out["error"] == "task.invalid"
    out = w.data_evict(ref, at="local")        # no copy recorded there
    assert out["error"] == "data.missing"
