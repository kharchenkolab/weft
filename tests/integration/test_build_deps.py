"""Eight-asks round D — RHEL10/toolchain. The consumer's ABI analysis
(PPM linux binaries target the distro's system R + libs; weft's R is
conda-forge's) resolved as: NO rhel whitelist extension; instead (D1)
a site policy lever ppm_binaries:false forcing plain source
everywhere, (D2) the binary load-check + per-package source rebuild
arm ported to the overlay lane (realize_layer had it; realize_overlay
verified by PRESENCE only — a broken-ABI binary passed), and (D3)
build_deps: headers/libs for source compiles on read-only/cold bases
(the lzma.h failure) via a deps-extended toolchain prefix — the
shared default toolchain never grows (its id keys every compile
cache), and the snapshot folds build_deps into deps.conda because the
compiled .so links them at RUNTIME."""

import pytest

from weft.api import Weft
from weft.solvers import site_ppm_url
from weft.toolchain import TOOLCHAIN_SPEC, toolchain_id


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


PPM = "https://packagemanager.posit.co/cran/2026-01-15"


class _Ad:
    def __init__(self, os_release=("ubuntu", "jammy"), binaries=True):
        self._weft_os_release = os_release
        self._weft_ppm_binaries = binaries


def test_ppm_binaries_false_forces_plain_source():
    assert "__linux__/jammy" in site_ppm_url(PPM, _Ad())
    assert site_ppm_url(PPM, _Ad(binaries=False)) == PPM   # untouched
    # non-PPM urls never touched either way
    assert site_ppm_url("https://cloud.r-project.org",
                        _Ad(binaries=False)) == "https://cloud.r-project.org"


def test_policy_stamps_the_adapter(w, tmp_path):
    w.register_site("nobin", "local", {
        "root": str(tmp_path / "s2"),
        "policy": {"ppm_binaries": False}})
    assert w.adapters["nobin"]._weft_ppm_binaries is False
    assert w.adapters["local"]._weft_ppm_binaries is True   # default


def test_overlay_lane_carries_the_load_check():
    """D2: both cran realize lanes carry the loadNamespace + source-
    rebuild arm — the overlay lane verified by presence only (a
    broken-ABI binary passed). Pinned on the source text: the arm has
    ONE recipe; a lane losing it drifts silently."""
    import inspect

    from weft.solvers import CranSolver
    layer_src = inspect.getsource(CranSolver.realize_layer)
    overlay_src = inspect.getsource(CranSolver.realize_overlay)
    for src, lane in ((layer_src, "realize_layer"),
                      (overlay_src, "realize_overlay")):
        assert "loadNamespace" in src, f"{lane} lost the load check"
        assert "rebuilding from source" in src, lane
        assert 'sub("__linux__/[^/]+/", ""' in src, lane


def test_toolchain_id_extras_fork_the_prefix_not_the_default():
    base = toolchain_id("linux-64")
    extended = toolchain_id("linux-64", ("xz",))
    assert base != extended                       # separate prefix
    assert toolchain_id("linux-64", ()) == base   # default UNCHANGED
    # order/duplication insensitive; content sensitive
    assert toolchain_id("linux-64", ("xz", "zlib")) == \
        toolchain_id("linux-64", ("zlib", "xz", "xz"))
    assert toolchain_id("linux-64", ("zlib",)) != extended


def test_build_deps_recorded_and_folded_into_snapshot_spec(w, monkeypatch):
    w.store._write(
        "INSERT INTO sessions(session_id, base_env_id, site, location,"
        " added_conda, added_pypi, state, created_at)"
        " VALUES('ses_bd','env:v1:b','local','sessions/ses_bd',"
        "'[]','[]','active',0)")
    out = w.sessions._install_inner(
        "ses_bd", w.adapters["local"], build_deps=["xz"])
    assert out["build_deps"] == ["xz"]
    assert "toolchain" in out["note"]
    # merge is set-union across calls
    w.sessions._install_inner("ses_bd", w.adapters["local"],
                              build_deps=["zlib", "xz"])
    s = w.store.get_session("ses_bd")
    assert s["build_deps"] == ["xz", "zlib"]
    # the snapshot spec carries them as REAL conda deps (runtime .so
    # linkage), deduped against added_conda
    monkeypatch.setattr(w.sessions.store, "get_env",
                        lambda eid: {"spec_hash": "spec:v1:p"})
    spec = w.sessions._synth_spec(s)
    assert set(spec["deps"]["conda"]) == {"xz", "zlib"}


def test_build_deps_alone_needs_no_packages(w):
    """build_deps without conda/pypi/cran records + prepares instead of
    refusing 'nothing to install'."""
    w.store._write(
        "INSERT INTO sessions(session_id, base_env_id, site, location,"
        " added_conda, added_pypi, state, created_at)"
        " VALUES('ses_only','env:v1:b','local','sessions/ses_only',"
        "'[]','[]','active',0)")
    out = w.sessions._install_inner("ses_only", w.adapters["local"],
                                    build_deps=["xz"])
    assert "error" not in out and out["build_deps"] == ["xz"]
    with pytest.raises(Exception):                # still refused bare
        w.sessions._install_inner("ses_only", w.adapters["local"])


def test_syslib_hint_names_the_lever():
    from weft.session import _syslib_hints
    got = _syslib_hints("fatal error: lzma.h: No such file or directory")
    assert got and got["failure_class"] == "missing_system_lib"
    assert "build_deps" in got["remedy"]


def test_prelude_serves_toolchain_include_and_lib(tmp_path):
    from weft.toolchain import build_env_prelude

    class Ad:
        def path(self, rel):
            return f"/site/{rel}"
    text = build_env_prelude(Ad(), "/site/toolchain/abc", "/site/envs/p")
    tc = "/site/toolchain/abc/.pixi/envs/default"
    parent = "/site/envs/p/.pixi/envs/default"
    # parent FIRST (its ABI wins), toolchain after; rpath for runtime
    assert text.index(f"-I{parent}/include") < text.index(f"-I{tc}/include")
    assert f"-Wl,-rpath,{tc}/lib" in text
    assert f"{tc}/lib/pkgconfig" in text
