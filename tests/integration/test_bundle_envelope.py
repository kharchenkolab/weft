"""Host metadata envelope on bundles (weft-ui ask): a caller-owned
payload weft stores and returns verbatim but never parses — and that
never enters the bundle's identity or re-derivation."""

import json
import tarfile

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def _done_job(w):
    r = w.task_submit({"command": "echo 7 > results/x.txt",
                       "outputs": ["results/"], "site": "local"})
    j = w.runner.wait(r["job_id"], 120)
    assert j["state"] == "DONE", j["error"]
    return r["job_id"]


def test_json_envelope_round_trips_verbatim(w, tmp_path, pixi_bin):
    payload = {"aba": {"entities": [{"kind": "claim", "id": "c-17"}],
                       "schema": "aba-lineage:v3"},
               "nested": [1, 2, {"deep": None}]}
    job = _done_job(w)
    b = w.bundle_export(job, str(tmp_path / "b.tgz"), metadata=payload)
    assert b["metadata_bytes"] > 0

    w2 = Weft(tmp_path / "ws2", pixi_bin=pixi_bin)
    imp = w2.bundle_import(b["path"])
    assert imp["metadata"] == payload            # exact bytes back
    assert imp["target_job"] == job


def test_bytes_envelope_and_none_default(w, tmp_path):
    job = _done_job(w)
    blob = b"\x00\x01opaque\xff not utf-8 \xfe"
    b = w.bundle_export(job, str(tmp_path / "b1.tgz"), metadata=blob)
    assert w.bundle_import(b["path"])["metadata"] == blob
    b2 = w.bundle_export(job, str(tmp_path / "b2.tgz"))
    assert w.bundle_import(b2["path"])["metadata"] is None


def test_envelope_never_perturbs_the_record(w, tmp_path):
    """Sealed means sealed: the bundle manifest (the record — tasks,
    hashes, recorded outputs) is byte-identical with and without an
    envelope; only the separate archive member differs."""
    job = _done_job(w)
    b1 = w.bundle_export(job, str(tmp_path / "with.tgz"),
                         metadata={"host": "payload"})
    b2 = w.bundle_export(job, str(tmp_path / "without.tgz"))

    def record(path):
        with tarfile.open(path) as tar:
            names = set(tar.getnames())
            man = json.loads(tar.extractfile("bundle/manifest.json").read())
        man.pop("created_at")
        return names, man
    names1, man1 = record(b1["path"])
    names2, man2 = record(b2["path"])
    assert man1 == man2                          # identity untouched
    assert names1 - names2 == {"bundle/host-metadata.json"}
    assert "host-metadata" not in str(sorted(names2))


def test_envelope_refuses_unserializable_and_oversize(w, tmp_path):
    job = _done_job(w)
    r = w.bundle_export(job, str(tmp_path / "x.tgz"),
                        metadata={"f": lambda: 1})
    assert r["error"] == "task.invalid" and "JSON" in r["detail"]
    r = w.bundle_export(job, str(tmp_path / "y.tgz"),
                        metadata=b"\x00" * ((64 << 20) + 1))
    assert r["error"] == "task.invalid" and "DataRef" in r["detail"]
