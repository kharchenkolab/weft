"""Round B (2026-07 sweep): identity comes from real digests and fences
fail closed. A hash tool that produces nothing must never satisfy a
fence or mint a ref; a publish must not report success it cannot prove."""

import pytest

from weft.adapters.base import ShimResult
from weft.data import _require_digest
from weft.errors import WeftError
from weft.realize import _bin_digest, _fence_ok

REAL = "a" * 64


class _Ad:
    """Scriptable adapter: run_cmd answers from a list, records commands."""

    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.cmds = []

    def run_cmd(self, cmd, timeout=120.0):
        self.cmds.append(cmd)
        return self.answers.pop(0) if self.answers else ShimResult(0, "", "")

    def path(self, rel):
        return f"/site/{rel}"


# ── _bin_digest: only a real digest is evidence ────────────────────────────

def test_bin_digest_empty_output_is_unverifiable():
    """No hash tool / missing dir / pipeline failure — anything but a
    64-hex digest must never equal a recorded one ("none"=="none" was
    a disarmed fence for the darwin fence's whole life)."""
    ad = _Ad([ShimResult(1, "", "sh: sha256sum: command not found")])
    assert _bin_digest(ad, "envs/e", "prefix") == "unverifiable"


def test_bin_digest_non_hex_output_is_unverifiable():
    ad = _Ad([ShimResult(0, "unverifiable\n", "")])
    assert _bin_digest(ad, "envs/e", "prefix") == "unverifiable"


def test_bin_digest_real_dir_hashes_on_this_host(tmp_path):
    """The fence works on THIS platform — darwin has shasum, not
    sha256sum; the old pipeline returned 'none' here forever."""
    from weft.adapters.local import LocalAdapter
    d = tmp_path / "envs/e1/.pixi/envs/default/bin"
    d.mkdir(parents=True)
    (d / "tool").write_text("#!/bin/sh\n")
    got = _bin_digest(LocalAdapter("l", tmp_path), "envs/e1", "prefix")
    assert len(got) == 64
    int(got, 16)  # is hex
    # deterministic: same inventory, same digest
    assert _bin_digest(LocalAdapter("l", tmp_path), "envs/e1", "prefix") == got


# ── _fence_ok: fail-closed comparison ──────────────────────────────────────

def _must_not_recompute():
    raise AssertionError("no recorded fence -> recompute must be skipped")


def test_fence_skips_recompute_without_information():
    for recorded in (None, "", "none", "unverifiable"):
        assert _fence_ok(recorded, _must_not_recompute)


def test_fence_requires_reproduction_of_a_real_digest():
    assert _fence_ok(REAL, lambda: REAL)
    assert not _fence_ok(REAL, lambda: "b" * 64)
    # FAIL-CLOSED: cannot-verify is not verified
    assert not _fence_ok(REAL, lambda: "unverifiable")


# ── _require_digest: no ref without a real hash ────────────────────────────

def test_require_digest_accepts_real():
    assert _require_digest(REAL, "/data/x.bin", "hpc") == REAL


def test_require_digest_rejects_size_mint():
    """Both hash tools failing while wc succeeds used to shift the size
    into the digest slot — dref:<SIZE> minted as identity at rc 0."""
    with pytest.raises(WeftError) as ei:
        _require_digest("83886080", "/data/x.bin", "hpc")
    assert ei.value.code == "site.bootstrap_failed"
    assert "produced no digest" in ei.value.detail
    assert "shasum" in ei.value.hints["suggestion"]


def test_require_digest_rejects_empty():
    with pytest.raises(WeftError) as ei:
        _require_digest("", "/data/x.bin", "hpc")
    assert ei.value.code == "site.bootstrap_failed"


# ── register path: discriminating payloads per trigger (sweep #7, #23) ─────

def _local_w(tmp_path, pixi_bin):
    from weft.api import Weft
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def _intercept_hash(adapter, result):
    orig = adapter.run_cmd

    def wrapped(cmd, **kw):
        if "sha256sum" in cmd:
            return result
        return orig(cmd, **kw)

    adapter.run_cmd = wrapped


def test_register_hashless_site_refuses_to_mint(tmp_path, pixi_bin):
    w = _local_w(tmp_path, pixi_bin)
    f = tmp_path / "site" / "big.bin"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"z" * 1024)
    # both hash tools "fail", wc still answers: the size lands first
    _intercept_hash(w._adapter("local"), ShimResult(0, " 1024 1752000000", ""))
    r = w.data_register(path=str(f), site="local")   # tool boundary: dict
    assert r["error"] == "site.bootstrap_failed"
    assert "produced no digest" in r["detail"]


def test_register_unreadable_file_is_not_a_wrong_path(tmp_path, pixi_bin):
    """The probe PROVED the path exists; a hash failure must not claim
    'no such file' and send the agent chasing a typo."""
    w = _local_w(tmp_path, pixi_bin)
    f = tmp_path / "site" / "locked.bin"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"z")
    _intercept_hash(w._adapter("local"),
                    ShimResult(1, "", "sha256sum: locked.bin: Permission denied"))
    r = w.data_register(path=str(f), site="local")   # tool boundary: dict
    assert r["error"] == "data.missing"
    assert "it exists" in r["detail"]
    assert "Permission denied" in r["hints"]["detail"]


# ── publish: no unproven success ───────────────────────────────────────────

class _PubAd(_Ad):
    def write_file(self, rel, data, mode=None):
        self.wrote = (rel, data)


def test_catalog_mv_failure_is_loud():
    """publish used to report success with the catalog UNCHANGED on
    sticky/foreign-owned trees."""
    from weft.publish import _write_catalog
    ad = _PubAd([ShimResult(1, "", "mv: cannot overwrite 'catalog.json'"),
                 ShimResult(0, "", "")])   # the mv, then the rm -f cleanup
    with pytest.raises(WeftError) as ei:
        _write_catalog(ad, "/tree", {"catalog_version": 1, "envs": {}})
    assert ei.value.code == "data.transfer_failed"
    assert "catalog" in ei.value.detail
    assert "cannot overwrite" in ei.value.hints["stderr"]
    assert any(c.startswith("rm -f") for c in ad.cmds)   # tmp not littered


def test_poisoned_compile_cache_entry_stops_shadowing(tmp_path):
    """cached_build returns the FIRST key match: a corrupt entry must be
    demotable, or it shadows every good re-cache forever (the decode-
    corrupted rlib tars did exactly that)."""
    from weft.store import Store
    from weft.toolchain import cached_build
    store = Store(tmp_path / "s.db")
    bad = "dref:" + "c" * 64
    store.put_dataref(bad, "file", 10, None, meta={"compile_cache": "K"})
    assert cached_build(store, "K") == bad
    store.update_dataref_meta(bad, {"compile_cache": None})   # the demotion
    assert cached_build(store, "K") is None


def test_publish_unmount_verified_gone():
    from weft.publish import _ensure_unmounted
    # busy unmount: fusermount exits 1 silently, mount still live
    ad = _Ad([ShimResult(0, "", ""), ShimResult(0, "live\n", "")])
    with pytest.raises(WeftError) as ei:
        _ensure_unmounted(ad, "/tree/envs/e1/mnt")
    assert ei.value.code == "state.conflict" and ei.value.retryable
    assert "EACCES" in ei.value.detail
    # clean unmount passes
    ad2 = _Ad([ShimResult(0, "", ""), ShimResult(0, "clear\n", "")])
    _ensure_unmounted(ad2, "/tree/envs/e1/mnt")
