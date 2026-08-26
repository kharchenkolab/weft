"""Bundle intake is a malformed-input boundary (field note #5 shape):
exporters are generators with their own bugs, and a bundle that imports
GREEN but crashes every later verb is the accept-and-mangle failure.
Vocabulary split pinned here: canonical["platforms"] is REQUIRED (the
lock's core — refuse at intake, naming the env and the field);
canonical["extras"] is OPTIONAL (get_env normalizes it at the one
hydration door, so no consumer needs tolerance)."""

import io
import json
import tarfile

import pytest

from weft.api import Weft

ENV = "env:v1:" + "b" * 64


@pytest.fixture
def w(tmp_path, pixi_bin):
    return Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")


def _bundle(tmp_path, envs):
    man = {"schema": "bundle:v1", "target_job": "jb_x",
           "jobs": {"jb_x": {"task": {"command": "true"},
                             "task_hash": "t" * 64,
                             "manifest": {}, "site": "local"}},
           "envs": envs, "datarefs": {}}
    p = tmp_path / "hostile.weft.tgz"
    with tarfile.open(p, "w:gz") as tar:
        mj = json.dumps(man).encode()
        info = tarfile.TarInfo("bundle/manifest.json")
        info.size = len(mj)
        tar.addfile(info, io.BytesIO(mj))
    return str(p)


def _env_entry(canonical):
    return {"spec_hash": "s" * 64, "spec_body": None,
            "canonical": canonical, "native_lock": "", "manifest": "",
            "platforms": ["linux-64"], "weakly_reproducible": False}


def test_missing_platforms_refused_typed_naming_the_env(w, tmp_path):
    out = w.bundle_import(_bundle(
        tmp_path, {ENV: _env_entry({"extras": {"modules": []}})}))
    assert out["error"] == "task.invalid"
    assert ENV in out["detail"] and "platforms" in out["detail"]
    assert out["hints"]["env_id"] == ENV
    assert w.store.get_env(ENV) is None, \
        "a refused env entry must not land in the store"


def test_non_dict_canonical_refused_not_crashed(w, tmp_path):
    out = w.bundle_import(_bundle(
        tmp_path, {ENV: _env_entry("not a lock document")}))
    assert out["error"] == "task.invalid"
    assert "canonical is str" in str(out["hints"]["present_keys"])


def test_sparse_extras_imports_and_renders(w, tmp_path):
    """extras missing is a SHIPPED shape (older exporters, adopt
    sidecars): the import lands, get_env hydrates extras as {}, and the
    render verbs work — the class that motivated this file was
    env_status answering internal.error forever on such a row."""
    out = w.bundle_import(_bundle(
        tmp_path, {ENV: _env_entry({"platforms": {"linux-64": []}})}))
    assert "error" not in out, out
    assert out["envs"] == [ENV]
    row = w.store.get_env(ENV)
    assert row["canonical"]["extras"] == {}, \
        "get_env owns extras normalization"
    st = w.env_status(ENV)
    assert "error" not in st, st
    assert st["summary"]["modules"] == []
