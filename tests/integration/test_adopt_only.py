"""The ADOPT-ONLY deployment shape (consumer report 2026-08-25):
adoption imports the env row (canonical + lock) but wrote no specs
row, so the snapshot's spec-hash extends raised "parent spec not
found" on every production-shaped install — sessions on adopted packs
could never be frozen, and every background job needing a snapshot
failed. It survived because every session/snapshot test (ours AND the
consumer's) ran on SOLVED workspaces, where env_ensure writes the
specs row as a side effect: the only configuration publish/adopt
ships was never a fixture. This file IS that fixture.

Fixes pinned here: (1) _synth_spec extends the ENV (extends_env),
which resolves off the row adoption does create — and is the more
correct snapshot: the base that RAN is the base that freezes;
(2) publish sidecars carry spec_body and adoption stores it, healing
the six get_spec consumers that silently degraded (verify: blocks
invisible at adopt/rebuild, nameless summaries, spec-less provenance,
revise refusals); (3) pre-spec_body trees adopt with an honest note.

The fixture: a REAL publish -> REAL adopt in a FRESH workspace. Only
the squashfs image build and its mount spot-check are stubbed (site
tooling the fast lane lacks); the sidecar/catalog emission and the
adopt consumption — the contract under test — run unmocked."""

import hashlib
import json
import platform as _platform
import sys
from pathlib import Path

import pytest

from weft.api import Weft
from weft.errors import WeftError


def _subdir() -> str:
    if sys.platform == "darwin":
        return "osx-arm64" if _platform.machine() == "arm64" else "osx-64"
    return ("linux-aarch64" if _platform.machine() in ("arm64", "aarch64")
            else "linux-64")


def _offline_env(w, tmp_path, name="pack-base", verify=False):
    chan = tmp_path / f"chan-{name}"
    sub = _subdir()
    for d in (chan / sub, chan / "noarch"):
        d.mkdir(parents=True, exist_ok=True)
    fn = f"{name}pkg-1.0-h0_0.conda"
    (chan / sub / "repodata.json").write_text(json.dumps(
        {"info": {"subdir": sub}, "packages": {}, "packages.conda": {
            fn: {"name": f"{name}pkg", "version": "1.0", "build": "h0_0",
                 "build_number": 0, "subdir": sub, "depends": [],
                 "sha256": hashlib.sha256(fn.encode()).hexdigest()}}}))
    (chan / "noarch" / "repodata.json").write_text(json.dumps(
        {"info": {"subdir": "noarch"}, "packages": {},
         "packages.conda": {}}))
    env = w.env_ensure({"name": name, "channels": [chan.as_uri()],
                        "platforms": [sub],
                        "deps": {"conda": [f"{name}pkg ==1.0"]},
                        **({"verify": {"loads": [f"{name}mod"]}}
                           if verify else {})})
    assert "error" not in env, env
    return env["env_id"]


def _adopt_ready(w, env_id, site="local"):
    """The production shape: the adopted pack is REALIZED in place
    (ro_roots/squashfs mount) — forge a ready prefix + marker so
    ensure_realization cache-hits and session_start needs no build."""
    from weft import realize as rm
    rel = rm.env_dir_rel(env_id)
    ad = w.adapters[site]
    bindir = Path(ad.path(rel)) / ".pixi/envs/default/bin"
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "tool").write_text("x")
    digest = rm._bin_digest(ad, rel, "prefix")
    ad.write_file(f"{rel}/.weft-ready", (json.dumps(
        {"strategy": "prefix", "bin_digest": digest}) + "\n").encode())
    (Path(ad.path(rel)) / "activate.sh").write_text("true\n")
    w.store.set_realization(env_id, site, "prefix", rel, "ready")


def _forge_squashfs_caps(w, site="local"):
    row = w.store.get_site(site)
    caps = dict(row.get("capabilities") or {})
    caps["squashfs"] = {"mksquashfs": True, "squashfuse": True,
                        "dev_fuse": True, "userns": True}
    w.store.set_capabilities(site, caps)


def _publish(w, env_id, tree, monkeypatch, name="pack", version="1.0"):
    """Real publish with ONLY the image build + mount spot-check
    stubbed — sidecar + catalog writes are the real emitters."""
    import weft.realize as realize_mod
    _forge_squashfs_caps(w)
    monkeypatch.setattr(
        realize_mod, "_build_squashfs",
        lambda *a, **kw: {"image_sha256": "e" * 64, "image_bytes": 128})
    monkeypatch.setattr(realize_mod, "_spot_check_and_mark",
                        lambda *a, **kw: None)
    out = w.env_publish(env_id, "local", str(tree), name, version)
    assert "error" not in out, out
    return out


@pytest.fixture
def published_tree(tmp_path, pixi_bin, monkeypatch):
    """Workspace A publishes; the TREE is the hand-off artifact."""
    wa = Weft(tmp_path / "ws-a", pixi_bin=pixi_bin, resume="off")
    wa.register_site("local", "local", {"root": str(tmp_path / "site-a"),
                                        "pixi_source": pixi_bin})
    env_id = _offline_env(wa, tmp_path)
    tree = tmp_path / "lab-tree"
    _publish(wa, env_id, tree, monkeypatch)
    return {"tree": tree, "env_id": env_id,
            "spec_hash": wa.store.get_env(env_id)["spec_hash"]}


@pytest.fixture
def wb(tmp_path, pixi_bin):
    """The FRESH adopt-only workspace — the shape consumers ship:
    zero specs rows, everything arrives through adoption."""
    w = Weft(tmp_path / "ws-b", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local", {"root": str(tmp_path / "site-b"),
                                       "pixi_source": pixi_bin})
    return w


def test_reporters_transcript_adopted_pack_is_freezable(published_tree,
                                                        wb):
    """THE TRANSCRIPT, replayed: fresh workspace adopts a published
    pack -> session_start -> session_freezable. On the reported code:
    freezable=false, task.invalid 'parent spec not found: <the env
    row's own spec_hash>', 0.0s."""
    got = wb.env_adopt("local", str(published_tree["tree"]), "pack")
    assert "error" not in got, got
    _adopt_ready(wb, got["env_id"])
    s = wb.session_start(got["env_id"], "local")
    assert "error" not in s, s
    out = wb.session_freezable(s["session_id"])
    assert out["freezable"] is True, out
    assert out["would_be_env"].startswith("env:")
    wb.session_stop(s["session_id"])


def test_adoption_stores_the_spec_row(tmp_path, pixi_bin, wb,
                                      monkeypatch):
    """spec_body rides the sidecar and adoption stores it: the six
    silently-degraded get_spec consumers heal — pinned on the worst
    one, the pack's verify: block, which was INVISIBLE on the
    consumer's own deployment (they added it specifically to catch
    the post-link class). This publish carries a verify block."""
    wa = Weft(tmp_path / "ws-a2", pixi_bin=pixi_bin, resume="off")
    wa.register_site("local", "local", {"root": str(tmp_path / "site-a2"),
                                        "pixi_source": pixi_bin})
    env_id = _offline_env(wa, tmp_path, name="vpack", verify=True)
    tree = tmp_path / "vtree"
    _publish(wa, env_id, tree, monkeypatch, name="vpack")
    spec_hash = wa.store.get_env(env_id)["spec_hash"]

    assert wb.store.get_spec(spec_hash) is None
    wb.env_adopt("local", str(tree), "vpack")
    body = wb.store.get_spec(spec_hash)
    assert body, "adoption left the spec_hash dangling"
    assert body["verify"]["loads"] == ["vpackmod"]
    # spec-hash extends resolves now too
    child = wb.env_ensure({"name": "child", "extends": spec_hash,
                           "platforms": [_subdir()],
                           "deps": {"conda": []}})
    assert "error" not in child, child


def test_pre_spec_body_tree_adopts_with_honest_note(published_tree, wb):
    """Old trees (no spec_body in the sidecar): adoption works, says
    what is degraded, and the snapshot path STILL works (extends_env
    needs no spec body)."""
    tree = published_tree["tree"]
    lock_file = next((tree / "locks").glob("*.json"))
    side = json.loads(lock_file.read_text())
    del side["spec_body"]                        # forge an old tree
    lock_file.write_text(json.dumps(side))
    got = wb.env_adopt("local", str(tree), "pack")
    assert "error" not in got, got
    assert "predates spec_body" in got.get("spec_note", "")
    assert wb.store.get_spec(published_tree["spec_hash"]) is None
    _adopt_ready(wb, got["env_id"])
    s = wb.session_start(got["env_id"], "local")
    out = wb.session_freezable(s["session_id"])
    assert out["freezable"] is True, out         # extends_env: no body needed
    wb.session_stop(s["session_id"])


def test_missing_parent_spec_hint_names_extends_env(wb, tmp_path):
    """The resolve_extends refusal now carries the lever an adopt-only
    deployment can actually pull."""
    out = wb.env_ensure({"name": "dangling",
                         "extends": "spec:v1:" + "0" * 64,
                         "deps": {"conda": []}})
    assert out["error"] == "task.invalid"
    assert "extends_env" in str(out["hints"].get("suggestion", ""))


def test_synth_spec_declares_parent_platforms(published_tree, wb):
    """A mac controller freezing a linux session must mint the linux
    env: the synth spec declares the PARENT's platforms."""
    got = wb.env_adopt("local", str(published_tree["tree"]), "pack")
    _adopt_ready(wb, got["env_id"])
    s = wb.session_start(got["env_id"], "local")
    spec = wb.sessions._synth_spec(wb.sessions._get(s["session_id"]))
    assert spec["extends_env"] == got["env_id"]
    assert spec["platforms"] == \
        wb.store.get_env(got["env_id"])["platforms"]
    wb.session_stop(s["session_id"])
