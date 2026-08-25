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


# MULTI-platform, deliberately (consumer audit #2 on this fixture):
# the ordinary published pack ships [linux-64, osx-arm64], and with a
# single-platform pack platforms[0] pinning is always accidentally
# right — the fixture could not falsify the claim. Each subdir gets a
# DIFFERENT build string so cross-platform pin poisoning cannot hide.
PACK_PLATFORMS = ["linux-64", "osx-arm64"]


def _offline_env(w, tmp_path, name="pack-base", verify=False):
    chan = tmp_path / f"chan-{name}"
    for sub, build in (("linux-64", "h_linux_0"),
                       ("osx-arm64", "h_osx_0")):
        (chan / sub).mkdir(parents=True, exist_ok=True)
        fn = f"{name}pkg-1.0-{build}.conda"
        (chan / sub / "repodata.json").write_text(json.dumps(
            {"info": {"subdir": sub}, "packages": {}, "packages.conda": {
                fn: {"name": f"{name}pkg", "version": "1.0",
                     "build": build, "build_number": 0, "subdir": sub,
                     "depends": [],
                     "sha256": hashlib.sha256(
                         fn.encode()).hexdigest()}}}))
    (chan / "noarch").mkdir(parents=True, exist_ok=True)
    (chan / "noarch" / "repodata.json").write_text(json.dumps(
        {"info": {"subdir": "noarch"}, "packages": {},
         "packages.conda": {}}))
    env = w.env_ensure({"name": name, "channels": [chan.as_uri()],
                        "platforms": PACK_PLATFORMS,
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
                           "platforms": PACK_PLATFORMS,
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


def _fake_parent(native_lock=None, spec_stored=False):
    return {
        "spec_hash": "spec:v1:parent",
        "platforms": ["linux-64", "osx-arm64"],
        "native_lock": native_lock,
        "canonical": {
            "platforms": {
                "linux-64": [
                    {"kind": "conda", "name": "zlib", "version": "1.3",
                     "build": "h_linux_1", "sha256": "a" * 64},
                    {"kind": "pypi", "name": "idna", "version": "3.7",
                     "build": "", "sha256": "b" * 64}],
                "osx-arm64": [
                    {"kind": "conda", "name": "zlib", "version": "1.3",
                     "build": "h_osx_9", "sha256": "c" * 64},
                    {"kind": "pypi", "name": "idna", "version": "3.7",
                     "build": "", "sha256": "d" * 64}]},
            "extras": {}},
    }


def test_pin_to_parent_pins_each_platform_from_its_own_lock(wb):
    """The mechanism pin: linux pins carry linux builds, osx pins carry
    osx builds — via per-target variants; shared deps hold ONLY the
    delta. platforms[0]'s build strings must never reach the other
    platform's solve."""
    from weft.spec import EnvSpec
    spec = EnvSpec.from_dict({
        "name": "snap", "extends_env": "env:v1:parent",
        "platforms": ["linux-64", "osx-arm64"],
        "deps": {"conda": ["newpkg"]}})
    out = wb.envman._pin_to_parent(spec, _fake_parent())
    assert out.conda == ["newpkg"]                     # delta only
    assert out.variants["linux-64"]["conda"] == \
        ["zlib ==1.3 h_linux_1"]
    assert out.variants["osx-arm64"]["conda"] == \
        ["zlib ==1.3 h_osx_9"]
    assert out.pypi == ["idna ==3.7"]                  # common version


def test_pin_to_parent_conflict_names_the_platform(wb):
    from weft.spec import EnvSpec
    spec = EnvSpec.from_dict({
        "name": "snap", "extends_env": "env:v1:parent",
        "platforms": ["linux-64", "osx-arm64"],
        "deps": {"conda": ["zlib >=2"]}})
    with pytest.raises(WeftError) as ei:
        wb.envman._pin_to_parent(spec, _fake_parent())
    e = ei.value
    assert e.code == "env.layer_conflict"
    assert e.hints["platform"] in ("linux-64", "osx-arm64")
    # adopt-only parent (no spec stored): the remedy must NOT send the
    # user through the door that raises parent-spec-not-found
    assert "re-ensure with `extends`" not in e.hints["suggestion"]
    assert "adopt a newer published version" in e.hints["suggestion"]


def test_pin_to_parent_inherits_channels_from_the_lock(wb):
    """Adopt-only (no parent spec row, canonical carries no channels):
    the channels that ACTUALLY solved the parent are in its lock —
    inherit from there, so channel_hint stops firing on a pack whose
    own spec lists bioconda."""
    import yaml
    from weft.spec import EnvSpec
    lock = yaml.safe_dump({
        "environments": {"default": {"channels": [
            {"url": "https://conda.anaconda.org/conda-forge/"},
            {"url": "https://conda.anaconda.org/bioconda/"}],
            "packages": {}}},
        "packages": []})
    spec = EnvSpec.from_dict({
        "name": "snap", "extends_env": "env:v1:parent",
        "platforms": ["linux-64", "osx-arm64"],
        "deps": {"conda": ["bioconductor-toolpkg"]}})
    from weft.envman import EnvManager
    assert EnvManager._channel_hint(spec) is not None  # falsifiable:
    out = wb.envman._pin_to_parent(spec, _fake_parent(native_lock=lock))
    assert "https://conda.anaconda.org/bioconda" in out.channels
    assert EnvManager._channel_hint(out) is None       # ...and healed


def test_solve_failure_remedy_discriminates_adopt_only(published_tree,
                                                       wb):
    """SIBLING of the layer_conflict remedy (class sweep, this round):
    the delta-does-not-fit handler had its own hardcoded 're-ensure
    with `extends` (the parent's SPEC hash)' — a door that raises
    parent-spec-not-found on an adopt-only workspace. A delta naming a
    package the channel does not carry drives the real solve failure.
    The parent is a PRE-spec_body tree: with the sidecar's spec_body
    adopted (the modern shape), re-ensure-with-extends is a door that
    opens and remains the right suggestion."""
    tree = published_tree["tree"]
    lock_file = next((tree / "locks").glob("*.json"))
    side = json.loads(lock_file.read_text())
    del side["spec_body"]
    lock_file.write_text(json.dumps(side))
    r = wb.env_adopt("local", str(tree), "pack")
    assert "error" not in r, r
    got = wb.env_ensure({"name": "snap-broken",
                         "extends_env": r["env_id"],
                         "platforms": PACK_PLATFORMS,
                         "deps": {"conda": ["weft-no-such-pkg"]}})
    assert got["error"] == "env.layer_conflict", got
    sug = got["hints"]["suggestion"]
    assert "re-ensure with `extends` (the parent's SPEC hash)" not in sug
    assert "adopt a newer published version" in sug


def test_channel_hint_sees_variant_deps(wb):
    """Class sweep: _channel_hint scanned only shared deps — a
    hand-authored [target.<plat>] naming bioconductor-* without
    bioconda got no pointer to the real cause."""
    from weft.envman import EnvManager
    from weft.spec import EnvSpec
    spec = EnvSpec.from_dict({
        "name": "t", "platforms": ["linux-64"], "deps": {"conda": []},
        "variants": {"linux-64": {"conda": ["bioconductor-toolpkg"]}}})
    hint = EnvManager._channel_hint(spec)
    assert hint and "bioconductor-toolpkg" in hint["packages"]


def test_find_near_sees_variant_deps(tmp_path, pixi_bin):
    """Class sweep: find_near's want-map read only shared deps — a spec
    expressing its asks per-platform ([target.<plat>]) built an empty
    want and matched NOTHING. Also this verb's first fast-lane test
    (edit => coverage; the allowlist only shrinks)."""
    w = Weft(tmp_path / "ws-n", pixi_bin=pixi_bin, resume="off")
    env_id = _offline_env(w, tmp_path, name="near")
    got = w.env_find_near({
        "name": "q", "platforms": PACK_PLATFORMS,
        "deps": {"conda": []},
        "variants": {"linux-64": {"conda": ["nearpkg ==1.0"]}}})
    assert any(r["env_id"] == env_id for r in got), got
