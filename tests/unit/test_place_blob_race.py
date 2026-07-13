"""Regression: concurrent staging of one blob must never collide.

weft-ui found the original bug in the wild: a 24-element array mounting
the same ref raced two stagers in local-link; the loser's os.link hit
EEXIST on the SHARED tmp name, fell back to copy2, and copy2 refused to
copy the source onto its own fresh hardlink (SameFileError) — surfaced
as a non-retryable internal error.
"""

import os
from concurrent.futures import ThreadPoolExecutor

from weft.cas import LocalCAS, place_blob
from weft.transfer.local_link import LocalLink


def test_place_blob_ignores_leftover_shared_tmp(tmp_path):
    """The exact pre-collision state of the old bug: a `<dst>.tmp` that is
    already a hardlink of src (the other racer's link). place_blob must
    neither trip over it nor corrupt the result."""
    src = tmp_path / "src"
    src.write_bytes(b"payload" * 100)
    dst = tmp_path / "out" / "blob"
    dst.parent.mkdir()
    stale = dst.with_suffix(".tmp")
    os.link(src, stale)  # what the losing racer used to find
    place_blob(src, dst)
    assert dst.read_bytes() == src.read_bytes()


def test_place_blob_concurrent_hammer(tmp_path):
    src = tmp_path / "src"
    src.write_bytes(b"x" * 4096)
    failures = []

    def stage(dst):
        try:
            place_blob(src, dst)
        except Exception as e:  # noqa: BLE001 — the test IS the exception check
            failures.append(repr(e))

    for round_ in range(50):
        dst = tmp_path / "rounds" / str(round_) / "blob"
        dst.parent.mkdir(parents=True)
        with ThreadPoolExecutor(16) as ex:
            for _ in range(16):
                ex.submit(stage, dst)
        assert dst.read_bytes() == src.read_bytes()
    assert not failures, failures[:3]


def test_local_link_transfer_concurrent_same_blob(tmp_path):
    """The product path: many threads staging the identical CAS object to
    one endpoint (what concurrent array elements do)."""
    cas = LocalCAS(tmp_path / "cas")
    info = cas.put_bytes(b"shared-code-blob" * 500)
    digest = info.ref.split(":")[-1]
    endpoint = {"cas_root": str(tmp_path / "site-cas")}
    link = LocalLink()
    failures = []

    def stage():
        try:
            link.transfer([(digest, info.bytes)], cas, endpoint)
        except Exception as e:  # noqa: BLE001
            failures.append(repr(e))

    with ThreadPoolExecutor(24) as ex:
        for _ in range(24):
            ex.submit(stage)
    assert not failures, failures[:3]
    staged = tmp_path / "site-cas" / digest[:2] / digest
    assert staged.read_bytes() == b"shared-code-blob" * 500
    # no tmp litter left behind
    assert not list((tmp_path / "site-cas" / digest[:2]).glob("*.tmp*"))
