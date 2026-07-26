"""The ref-addressed observation tier (uniformity audit): every address
vocabulary gets observation verbs — data_stat (live vs record,
non-mutating) and data_read_range (same engine as run_file_read_range;
tree refs address members via rel=). One engine means the two range
verbs cannot drift — pinned by the conformance test at the bottom."""

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


def _file_ref(w, tmp_path, name="alpha.bin",
              data=b"abcdefghijklmnopqrstuvwxyz"):
    p = tmp_path / name
    p.write_bytes(data)
    return w.data_register(str(p))["ref"]


def _site_tree_ref(w, tmp_path, ingest=False):
    """A reference-in-place (or ingested) directory tree on the local
    site — the chunked-store shape."""
    store_dir = tmp_path / "site" / "perm" / "demo.zarr"
    (store_dir / "c" / "0").mkdir(parents=True)
    (store_dir / ".zattrs").write_text('{"k": 1}')
    (store_dir / "c" / "0" / "0").write_bytes(bytes(range(256)))
    (store_dir / "c" / "0" / "1").write_bytes(b"chunk-two" * 100)
    return w.data_register(str(store_dir), site="local",
                           ingest=ingest)["ref"]


# ── data_read_range ────────────────────────────────────────────────────────

def test_range_by_file_ref_from_workspace(w, tmp_path):
    ref = _file_ref(w, tmp_path)
    got = w.data_read_range(ref, offset=10, length=5)
    assert base64.b64decode(got["bytes_b64"]) == b"klmno"
    assert got["at"] == "workspace" and got["size"] == 26
    assert got["eof"] is False and got["ref"] == ref
    past = w.data_read_range(ref, offset=99)
    assert past["nbytes"] == 0 and past["eof"] is True and \
        past["size"] == 26


def test_range_by_tree_ref_member(w, tmp_path):
    """THE aba shape: a reference-in-place directory tree, member
    addressed by rel= — bytes served from the external home with no
    whole-file (or whole-tree) movement."""
    ref = _site_tree_ref(w, tmp_path, ingest=False)
    got = w.data_read_range(ref, rel="c/0/0", offset=250, length=6)
    assert base64.b64decode(got["bytes_b64"]) == bytes([250, 251, 252,
                                                        253, 254, 255])
    assert got["size"] == 256 and got["eof"] is True
    assert got["rel"] == "c/0/0" and got["at"] == "local"
    assert got["via"] == "external-home"


def test_range_tree_rel_intake_contract(w, tmp_path):
    tree = _site_tree_ref(w, tmp_path)
    fref = _file_ref(w, tmp_path)
    out = w.data_read_range(tree)                    # tree without rel
    assert out["error"] == "task.invalid" and "rel=" in out["detail"]
    out = w.data_read_range(tree, rel="c/9/9")       # absent member
    assert out["error"] == "data.missing"
    out = w.data_read_range(fref, rel="x")           # rel on a file ref
    assert out["error"] == "task.invalid"
    out = w.data_read_range("dref:" + "0" * 64)      # unknown ref
    assert out["error"] == "data.missing"
    out = w.data_read_range(tree, rel="../escape")   # traversal
    assert out["error"] in ("task.invalid", "data.missing")


def test_range_ref_site_narrowing_and_unknown_site(w, tmp_path):
    ref = _site_tree_ref(w, tmp_path)
    got = w.data_read_range(ref, rel=".zattrs", site="local")
    assert base64.b64decode(got["bytes_b64"]) == b'{"k": 1}'
    miss = w.data_read_range(ref, rel=".zattrs", site="nonexistent")
    assert miss["error"] == "data.missing"           # no candidates there


def test_range_by_ingested_tree_member_site_cas(w, tmp_path):
    """The other tree home: ingest=True hardlinks member blobs into
    the SITE CAS — members resolve by their own hashes there."""
    ref = _site_tree_ref(w, tmp_path, ingest=True)
    got = w.data_read_range(ref, rel="c/0/1", offset=0, length=9)
    assert base64.b64decode(got["bytes_b64"]) == b"chunk-two"
    assert got["size"] == 900
    assert got["via"] == "site-cas" and got["at"] == "local"


# ── data_stat ──────────────────────────────────────────────────────────────

def test_stat_file_ref_workspace_and_divergence(w, tmp_path):
    ref = _file_ref(w, tmp_path)
    st = w.data_stat(ref)
    assert st["workspace"]["present"] is True
    assert st["workspace"]["bytes"] == 26
    assert st["divergent"] is False
    # nuke the workspace blob: observation reports, does NOT demote
    from weft.cas import LocalCAS
    blob = w.cas._blob_path(ref[len("dref:"):])
    blob.unlink()
    st2 = w.data_stat(ref)
    assert st2["workspace"]["present"] is False
    assert st2["divergent"] is True
    assert "demoted" not in str(st2.get("note", "")) or \
        "nothing was demoted" in st2["note"]


def test_stat_tree_ref_external_home_sampled_honestly(w, tmp_path):
    ref = _site_tree_ref(w, tmp_path, ingest=False)
    st = w.data_stat(ref, sample=2)
    assert st["recorded"]["kind"] == "tree"
    assert st["recorded"]["files"] == 3
    site = next(s for s in st["sites"] if s["site"] == "local")
    assert site["via"] == "external-home"
    assert site["members_checked"] == 2
    assert site["members_present"] == 2
    assert site["sampled"] is True                   # 2 of 3: SAY so
    # delete one probed member at the home -> divergence, non-mutating
    (tmp_path / "site" / "perm" / "demo.zarr" / ".zattrs").unlink()
    st2 = w.data_stat(ref, sample=3)
    site2 = next(s for s in st2["sites"] if s["site"] == "local")
    assert site2["members_present"] < site2["members_checked"]
    assert st2["divergent"] is True
    # the location row survives untouched (observation, not verdict)
    assert w.store.locations_of(ref)


def test_stat_batch_refs_with_per_entry_errors(w, tmp_path):
    r1 = _file_ref(w, tmp_path, "a.bin", b"aaa")
    out = w.data_stat(refs=[r1, "dref:" + "f" * 64])
    assert out["refs"][r1]["workspace"]["present"] is True
    assert out["refs"]["dref:" + "f" * 64]["error"] == "data.missing"
    assert w.data_stat()["error"] == "task.invalid"          # neither
    assert w.data_stat(ref=r1, refs=[r1])["error"] == "task.invalid"


# ── ONE engine: the two range verbs cannot drift ───────────────────────────

def test_range_verbs_share_one_engine_conformance(w, tmp_path):
    """Identical bytes through both addressings produce identical range
    fields — the conformance pin that keeps run_file_read_range and
    data_read_range on the SAME engine semantics."""
    data = bytes(range(256)) * 8                     # 2 KiB
    jid = w.task_submit({
        "command": "python3 -c 'import sys; "
                   "sys.stdout.buffer.write(bytes(range(256))*8)' "
                   "> blob.bin && cp blob.bin out.bin",
        "outputs": ["out.bin"], "site": "local"})["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    ref = next(o["ref"] for o in
               w.task_result(jid)["outputs"] if o["path"] == "out.bin")
    for kwargs in ({"offset": 100, "length": 64},
                   {"offset": 2040, "length": 100},   # tail past EOF
                   {"offset": 5000},                  # fully past EOF
                   {"offset": 0, "length": 0}):
        via_run = w.run_file_stat(jid, rel="blob.bin") and \
            w.run_file_read_range(jid, "blob.bin", **kwargs)
        via_ref = w.data_read_range(ref, **kwargs)
        for k in ("offset", "nbytes", "size", "eof", "capped",
                  "bytes_b64"):
            assert via_run[k] == via_ref[k], (kwargs, k)
        assert base64.b64decode(via_ref["bytes_b64"]) == \
            data[kwargs["offset"]:kwargs["offset"]
                 + kwargs.get("length", len(data))]
