import os

import pytest

from weft.cas import LocalCAS, staging_plan
from weft.errors import WeftError
from weft.ids import CHUNK_SIZE, hash_file, hash_tree


@pytest.fixture
def cas(tmp_path):
    return LocalCAS(tmp_path / "cas")


def test_register_file_and_fastpath(cas, tmp_path):
    f = tmp_path / "run2189.dat"
    f.write_bytes(b"beam position samples\n" * 1000)
    info1 = cas.register_file(f)
    assert info1.ref.startswith("dref:") and info1.kind == "file"
    # second registration hits the mtime+size fast path and agrees
    info2 = cas.register_file(f)
    assert info2.ref == info1.ref
    # content change changes the ref even with same length
    data = bytearray(f.read_bytes())
    data[0] ^= 0xFF
    f.write_bytes(bytes(data))
    assert cas.register_file(f).ref != info1.ref


def test_tree_hash_ignores_nothing_and_orders(cas, tmp_path):
    d = tmp_path / "proj"
    (d / "sub").mkdir(parents=True)
    (d / "a.py").write_text("print('scan')\n")
    (d / "sub" / "b.cfg").write_text("grid=2000\n")
    info = cas.register_tree(d)
    assert info.kind == "tree"
    # tree identity is content-derived: rebuilding identical content elsewhere matches
    d2 = tmp_path / "proj2"
    (d2 / "sub").mkdir(parents=True)
    (d2 / "a.py").write_text("print('scan')\n")
    (d2 / "sub" / "b.cfg").write_text("grid=2000\n")
    assert cas.register_tree(d2).ref == info.ref
    # touching a file's content changes the tree ref
    (d2 / "a.py").write_text("print('scan v2')\n")
    assert cas.register_tree(d2).ref != info.ref


def test_materialize_roundtrip(cas, tmp_path):
    d = tmp_path / "code"
    d.mkdir()
    (d / "run.sh").write_text("#!/bin/sh\necho hi\n")
    os.chmod(d / "run.sh", 0o755)
    (d / "data.txt").write_text("x")
    info = cas.register_tree(d)
    out = tmp_path / "sandbox" / "code"
    cas.materialize(info.ref, out)
    assert (out / "data.txt").read_text() == "x"
    assert os.access(out / "run.sh", os.X_OK)
    assert hash_tree(out)[0] == hash_tree(d)[0]


def test_verify_detects_corruption(cas, tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"payload")
    info = cas.register_file(f)
    assert cas.verify(info.ref)
    blob = cas.open_blob(info.ref)
    os.chmod(blob, 0o644)
    with open(blob, "r+b") as fh:
        fh.write(b"X")
    assert not cas.verify(info.ref)


def test_missing_ref_raises_structured(cas):
    with pytest.raises(WeftError) as e:
        cas.open_blob("dref:" + "0" * 64)
    assert e.value.code == "data.missing"


def test_chunked_hashing_boundary(tmp_path):
    small = tmp_path / "small.bin"
    small.write_bytes(b"a" * (CHUNK_SIZE - 1))
    assert hash_file(small).chunks is None
    big = tmp_path / "big.bin"
    big.write_bytes(b"a" * (CHUNK_SIZE + 1))
    d = hash_file(big)
    assert d.chunks is not None and len(d.chunks) == 2
    assert d.size == CHUNK_SIZE + 1


def test_staging_plan_set_difference():
    sizes = {"dref:aa": 100, "dref:bb": 2_000_000_000, "dref:cc": 5}
    plan = staging_plan(
        required=["dref:aa", "dref:bb", "dref:cc", "dref:bb"],  # dup collapses
        present_at_target={"dref:aa", "dref:cc"},
        sizes=sizes,
    )
    assert plan.to_transfer == ["dref:bb"]
    assert plan.present == ["dref:aa", "dref:cc"]
    assert plan.bytes_to_move == 2_000_000_000
