"""Leftovers round (aba weft_ask_combined + 4-class sweep): in-job
namespace collisions refuse at submit, tree member sizes are FACTS,
declared mime, one origin parser."""

import pytest

from weft.api import Weft
from weft.errors import WeftError


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def _ref(w, tmp_path, name="a.bin", content=b"payload"):
    p = tmp_path / name
    p.write_bytes(content)
    return w.data_register(str(p))["ref"]


# -- item 3 + siblings: one in-job path, one writer ---------------------------

def test_duplicate_input_mounts_refused(w, tmp_path):
    """The live campaign shape: two inputs land on 'status.json' and
    silently clobber — the model needed a probe job to discover it."""
    r1 = _ref(w, tmp_path, "x1.bin", b"one")
    r2 = _ref(w, tmp_path, "x2.bin", b"two")
    out = w.task_submit({"command": "true", "site": "local", "inputs": [
        {"ref": r1, "mount_as": "status.json"},
        {"ref": r2, "mount_as": "status.json"}]})
    assert out.get("error") == "task.invalid", out
    assert "inputs[0]" in out["detail"] and "inputs[1]" in out["detail"]
    assert "mount_as" in out["hints"]["suggestion"]


def test_duplicate_outputs_refused(w):
    out = w.task_submit({"command": "true", "site": "local",
                         "outputs": ["r.txt", "r.txt"]})
    assert out.get("error") == "task.invalid"
    assert "declared twice" in out["detail"]


def test_output_at_input_mount_refused(w, tmp_path):
    """Worse than a collision: inputs are hardlink-shared read-only —
    an output written there can falsify the content-addressed record
    (the data-doctrine hazard, now guarded at intake)."""
    r1 = _ref(w, tmp_path)
    out = w.task_submit({"command": "true", "site": "local",
                         "inputs": [{"ref": r1, "mount_as": "d/in.bin"}],
                         "outputs": ["d/in.bin"]})
    assert out.get("error") == "task.invalid"
    assert "read-only" in out["detail"]


def test_weft_env_namespace_reserved(w):
    out = w.task_submit({"command": "true", "site": "local",
                         "env_vars": {"WEFT_CPUS": "64"}})
    assert out.get("error") == "task.invalid"
    assert out["hints"]["reserved"] == "WEFT_*"
    # spec-level twin (same clobber through the env layer)
    out = w.env_ensure({"name": "x", "deps": {"conda": ["python"]},
                        "env_vars": {"WEFT_JOB_ID": "spoof"}})
    assert out.get("error") == "task.invalid"


def test_arrays_still_get_weft_array_index(w):
    """WEFT_ARRAY_INDEX is exempt from the reserved-namespace gate:
    weft's own array merge stamps it and the job row ROUND-TRIPS
    through from_dict at drive time — first version of the gate refused
    weft's own stamp and every array element failed (caught here)."""
    group = w.task_submit({"command": "echo $WEFT_ARRAY_INDEX > out.txt",
                           "outputs": ["out.txt"], "site": "local",
                           "array": 2})["group"]
    import time
    deadline = time.time() + 120
    while time.time() < deadline:
        st = w.array_status(group)
        if st["done"] + st["failed"] == 2:
            break
        time.sleep(0.3)
    assert w.array_status(group)["done"] == 2


def test_distinct_mounts_still_run(w, tmp_path):
    r1 = _ref(w, tmp_path, "y1.bin", b"one")
    r2 = _ref(w, tmp_path, "y2.bin", b"two")
    jid = w.task_submit({"command": "cat a/status.json b/status.json > out.txt",
                         "site": "local", "outputs": ["out.txt"],
                         "inputs": [
                             {"ref": r1, "mount_as": "a/status.json"},
                             {"ref": r2, "mount_as": "b/status.json"}],
                         })["job_id"]
    job = w.runner.wait(jid, 120)
    assert job["state"] == "DONE", job.get("error")


# -- item 2: member sizes are facts -------------------------------------------

def test_data_members_serves_real_sizes(w, tmp_path):
    d = tmp_path / "tree"
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_bytes(b"x" * 111)
    (d / "sub" / "b.bin").write_bytes(b"y" * 2048)
    (d / "lnk").symlink_to("a.txt")
    ref = w.data_register(str(d))["ref"]
    members = {m["path"]: m for m in w.data_members(ref)["members"]}
    # FACTS, not shape: the old test asserted the key existed and passed
    # while every value was None (aba's conformance freeze caught it)
    assert members["a.txt"]["bytes"] == 111
    assert members["sub/b.bin"]["bytes"] == 2048
    link = members["lnk"]
    assert link["kind"] == "link"
    assert "bytes" not in link and "sha256" not in link, \
        "links drop the keys — absent over always-None"


# -- item 1: declared mime ----------------------------------------------------

def test_mime_declared_and_served(w, tmp_path):
    p = tmp_path / "table.csv"
    p.write_text("a,b\n1,2\n")
    out = w.data_register(str(p), mime="text/csv")
    assert out["mime"] == "text/csv"
    ref = out["ref"]
    assert w.data_describe(ref)["meta"]["mime"] == "text/csv"
    rows = {r["ref"]: r for r in w.data_list()["refs"]}
    assert rows[ref]["meta"]["mime"] == "text/csv"
    # re-declaration wins (annotation semantics, like notes)
    w.data_register(str(p), mime="text/plain")
    assert w.data_describe(ref)["meta"]["mime"] == "text/plain"
    # absence stays honest
    q = tmp_path / "blob.bin"
    q.write_bytes(b"\x00\x01")
    ref2 = w.data_register(str(q))["ref"]
    assert "mime" not in (w.data_describe(ref2)["meta"] or {})


def test_mime_hygiene_refusals(w, tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"z")
    for bad in ("csv", "text/", "/csv", "a/b/c", "text/cs v",
                "x" * 128 + "/y", "text/\x07csv"):
        out = w.data_register(str(p), mime=bad)
        assert out.get("error") == "task.invalid", (bad, out)
        assert "type/subtype" in out["detail"]


# -- origin: one parser -------------------------------------------------------

ORIGIN_CASES = [
    ("job:jobs/jb_abc123", {"kind": "job", "job_id": "jb_abc123"}),
    ("run:jb_x/results/out.h5",
     {"kind": "run", "target": "jb_x", "rel": "results/out.h5"}),
    ("run:kr_y/a.txt", {"kind": "run", "target": "kr_y", "rel": "a.txt"}),
    ("post_install: pip install ./pkg",
     {"kind": "post_install", "detail": " pip install ./pkg"}),
    ("https://example.org/d.h5",
     {"kind": "opaque", "detail": "https://example.org/d.h5"}),
    ("", {"kind": "opaque", "detail": ""}),
]


@pytest.mark.parametrize("origin,want", ORIGIN_CASES)
def test_parse_origin_conformance(w, origin, want):
    assert w.dataman.parse_origin(origin) == want


def test_provenance_still_walks_through_origins(w, tmp_path):
    """The converted call sites against the REAL consumer: a job output
    re-registered by (run, rel) walks back to the producing job."""
    jid = w.task_submit({"command": "echo deep > results/x.txt",
                         "outputs": ["results/"], "site": "local"})["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    reg = w.data_register(run=jid, rel="results/x.txt")
    p = w.provenance(reg["ref"])
    chain = p.get("produced_by") or {}
    assert chain.get("job_id") == jid or jid in str(p), p
