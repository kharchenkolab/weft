"""Extends survives channel rotation (eight-asks round B, keystone):
parent_pins feeds `name ==version build` into a LIVE repodata solve, so
when a channel rotates a build away (bioconda prunes; aba2's
"No candidates ... r44hdfd78af_0"), every extends/overlay/session solve
on that parent fails forever — published packs had a shelf life of
weeks. Now the parent's OWN lock is synthesized into a local channel
for the solve (it records name/version/build/depends/sha256 —everything
repodata needs), and the resulting lock's file:// URLs are rewritten
back to the parent's real ones (a remote realize cannot fetch the
controller's disk). Identity is untouched: same filenames, same shas —
pinned here by field equality with the parent lock.

All tests run against LOCAL file:// channels: `pixi lock` resolves from
repodata alone (no artifacts fetched), so the rotation is a repodata
edit we control and the lane needs no network."""

import hashlib
import json
import platform as _platform
import sys
import yaml
from pathlib import Path

import pytest

from weft.api import Weft
from weft.envman import EnvManager


def _subdir() -> str:
    if sys.platform == "darwin":
        return "osx-arm64" if _platform.machine() == "arm64" else "osx-64"
    return ("linux-aarch64" if _platform.machine() in ("arm64", "aarch64")
            else "linux-64")


def _mk_channel(root: Path, packages: dict[str, list[tuple[str, str]]]):
    """packages: {name: [(version, build), ...]} -> a real repodata-only
    conda channel at root (current subdir + noarch)."""
    sub = _subdir()
    for d in (root / sub, root / "noarch"):
        d.mkdir(parents=True, exist_ok=True)
    entries = {}
    for name, builds in packages.items():
        for version, build in builds:
            fn = f"{name}-{version}-{build}.conda"
            entries[fn] = {
                "name": name, "version": version, "build": build,
                "build_number": 0, "subdir": sub, "depends": [],
                "sha256": hashlib.sha256(fn.encode()).hexdigest(),
                "size": 1000}
    (root / sub / "repodata.json").write_text(json.dumps(
        {"info": {"subdir": sub}, "packages": {},
         "packages.conda": entries}))
    (root / "noarch" / "repodata.json").write_text(json.dumps(
        {"info": {"subdir": "noarch"}, "packages": {},
         "packages.conda": {}}))
    return root.as_uri()


@pytest.fixture
def w(tmp_path, pixi_bin):
    return Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")


def _lock_entry(native_lock: str, name: str) -> dict:
    doc = yaml.safe_load(native_lock)
    for rec in doc.get("packages", []):
        url = rec.get("conda") or ""
        if f"/{name}-" in url:
            return {"url": url, "sha256": rec.get("sha256")}
    raise AssertionError(f"{name} not in lock")


def _in_channel(entry_url: str, channel_uri: str) -> bool:
    # pixi records file-channel packages as BARE paths; channel URIs
    # keep file:// — accept either form for the fixture channel
    return entry_url.startswith(channel_uri) or \
        entry_url.startswith(channel_uri[len("file://"):])


def test_extends_survives_build_rotation(w, tmp_path):
    """THE case: parent solved when its build existed; the channel
    rotates it away; the extends solve must still succeed, and the
    child lock's parent entry must be FIELD-IDENTICAL to the parent's
    (same real URL, same sha — identity equality, the strongest pin)."""
    chan = tmp_path / "chan"
    url = _mk_channel(chan, {"weftpkg": [("1.0", "h000_0")],
                             "weftextra": [("2.0", "h111_0")]})
    parent = w.env_ensure({"name": "rot-parent", "channels": [url],
                           "platforms": [_subdir()],
                           "deps": {"conda": ["weftpkg ==1.0"]}})
    assert "error" not in parent, parent
    parent_lock = w.store.get_env(parent["env_id"])["native_lock"]
    p_entry = _lock_entry(parent_lock, "weftpkg")
    assert _in_channel(p_entry["url"], url)        # real (test) channel

    # ROTATION: weftpkg 1.0 h000_0 vanishes from repodata
    _mk_channel(chan, {"weftpkg": [("1.1", "h999_1")],
                       "weftextra": [("2.0", "h111_0")]})

    child = w.env_ensure({"name": "rot-child", "channels": [url],
                          "platforms": [_subdir()],
                          "extends_env": parent["env_id"],
                          "deps": {"conda": ["weftextra ==2.0"]}})
    assert "error" not in child, child             # RED on HEAD: layer_conflict
    child_lock = w.store.get_env(child["env_id"])["native_lock"]
    c_entry = _lock_entry(child_lock, "weftpkg")
    assert c_entry == p_entry                      # identity equality
    assert "parent-channel" not in child_lock      # rewrite left nothing
    assert _in_channel(_lock_entry(child_lock, "weftextra")["url"], url)


def test_child_channels_inherit_from_parent_spec(w, tmp_path):
    """aba2's #3 root cause, closed on the weft side for extends_env:
    the delta package lives ONLY on the parent's channel; the child
    spec doesn't list it — inheritance makes the delta solve see it."""
    chan = tmp_path / "chan2"
    url = _mk_channel(chan, {"basepkg": [("1.0", "h0_0")],
                             "deltapkg": [("3.0", "h5_0")]})
    parent = w.env_ensure({"name": "inh-parent", "channels": [url],
                           "platforms": [_subdir()],
                           "deps": {"conda": ["basepkg ==1.0"]}})
    assert "error" not in parent, parent
    child = w.env_ensure({"name": "inh-child",
                          "platforms": [_subdir()],   # NO channels named
                          "extends_env": parent["env_id"],
                          "deps": {"conda": ["deltapkg ==3.0"]}})
    assert "error" not in child, child
    assert _in_channel(_lock_entry(
        w.store.get_env(child["env_id"])["native_lock"],
        "deltapkg")["url"], url)


def test_legacy_parent_without_lock_gets_rotation_hint(w, tmp_path):
    chan = tmp_path / "chan3"
    url = _mk_channel(chan, {"oldpkg": [("1.0", "h0_0")]})
    parent = w.env_ensure({"name": "leg-parent", "channels": [url],
                           "platforms": [_subdir()],
                           "deps": {"conda": ["oldpkg ==1.0"]}})
    assert "error" not in parent
    # forge a legacy row (pre-native_lock) + rotate the build away
    w.store._write("UPDATE envs SET native_lock=NULL WHERE env_id=?",
                   (parent["env_id"],))
    _mk_channel(chan, {"oldpkg": [("2.0", "h9_1")]})
    child = w.env_ensure({"name": "leg-child", "channels": [url],
                          "platforms": [_subdir()],
                          "extends_env": parent["env_id"],
                          "deps": {"conda": []}})
    assert child["error"] == "env.layer_conflict"
    assert "rotated" in str(child["hints"].get("rotation", "")) or \
        "rotation" in child["hints"], child["hints"]


def test_channel_hint_fires_on_bioconductor_without_bioconda():
    from weft.spec import EnvSpec
    hint = EnvManager._channel_hint(EnvSpec.from_dict(
        {"name": "x", "channels": ["conda-forge"],
         "deps": {"conda": ["bioconductor-deseq2", "r-base =4.4"]}}))
    assert hint and hint["packages"] == ["bioconductor-deseq2"]
    assert "bioconda" in hint["fix"]
    # with bioconda present: silent
    assert EnvManager._channel_hint(EnvSpec.from_dict(
        {"name": "x", "channels": ["conda-forge", "bioconda"],
         "deps": {"conda": ["bioconductor-deseq2"]}})) is None
    # plain r-* alone never fires (conda-forge carries r-*)
    assert EnvManager._channel_hint(EnvSpec.from_dict(
        {"name": "x", "channels": ["conda-forge"],
         "deps": {"conda": ["r-jsonlite"]}})) is None


def test_channel_hint_attaches_to_ensure_result(w, tmp_path):
    chan = tmp_path / "chan4"
    url = _mk_channel(chan, {"plainpkg": [("1.0", "h0_0")]})
    out = w.env_ensure({"name": "no-hint", "channels": [url],
                        "platforms": [_subdir()],
                        "deps": {"conda": ["plainpkg ==1.0"]}})
    assert "error" not in out and "channel_hint" not in out


def test_synth_channel_repodata_shape(tmp_path):
    """Unit conformance for the synthesized channel + URL map: fields
    verbatim from the lock; noarch always present; map keys are the
    file:// URLs pixi would record."""
    from weft.lock import synth_parent_channel
    lock = yaml.safe_dump({
        "version": 6,
        "environments": {"default": {"packages": {
            "linux-64": [{"conda": "https://conda.anaconda.org/bioconda/"
                                   "linux-64/bioconductor-x-1.68.0-"
                                   "r44hdfd78af_0.conda"}]}}},
        "packages": [{
            "conda": "https://conda.anaconda.org/bioconda/linux-64/"
                     "bioconductor-x-1.68.0-r44hdfd78af_0.conda",
            "sha256": "ab" * 32,
            "depends": ["r-base >=4.4,<4.5.0a0"]}]})
    url, url_map = synth_parent_channel(lock, tmp_path / "pc")
    repo = json.loads((tmp_path / "pc" / "linux-64" /
                       "repodata.json").read_text())
    entry = repo["packages.conda"][
        "bioconductor-x-1.68.0-r44hdfd78af_0.conda"]
    assert entry["version"] == "1.68.0"
    assert entry["build"] == "r44hdfd78af_0"
    assert entry["depends"] == ["r-base >=4.4,<4.5.0a0"]
    assert entry["sha256"] == "ab" * 32
    assert (tmp_path / "pc" / "noarch" / "repodata.json").exists()
    # BOTH url forms mapped (pixi records bare paths, channels file://)
    uri, bare = sorted(url_map, key=len, reverse=True)
    assert uri.startswith("file://") and uri.endswith(
        "/linux-64/bioconductor-x-1.68.0-r44hdfd78af_0.conda")
    assert not bare.startswith("file://") and bare.endswith(
        "/linux-64/bioconductor-x-1.68.0-r44hdfd78af_0.conda")
    assert all(v.startswith("https://conda.anaconda.org/")
               for v in url_map.values())


@pytest.mark.solver
def test_real_channel_extends_unperturbed(w, tmp_path):
    """Happy path against LIVE conda-forge: the synth channel is active
    (parent has a lock) but every build still exists upstream — the
    child lock must carry real https URLs only, parent entries
    field-identical. The synth machinery must be invisible when
    rotation hasn't happened."""
    parent = w.env_ensure({"name": "real-parent",
                           "platforms": [_subdir()],
                           "deps": {"conda": ["python =3.12"]}})
    assert "error" not in parent, parent
    child = w.env_ensure({"name": "real-child",
                          "platforms": [_subdir()],
                          "extends_env": parent["env_id"],
                          "deps": {"conda": ["ca-certificates"]}})
    assert "error" not in child, child
    child_lock = w.store.get_env(child["env_id"])["native_lock"]
    assert "parent-channel" not in child_lock
    p = _lock_entry(w.store.get_env(parent["env_id"])["native_lock"],
                    "python")
    assert _lock_entry(child_lock, "python") == p
    assert p["url"].startswith("https://")
