"""R/cran lane round (aba 1.2 measured note): delta-aware solve,
platform-aware PPM URLs, build parallelism, progress — plus the parser
and platform class-sweep fixes that fell out of the same OODA pass."""

import threading
import time

import pytest

from weft.errors import WeftError
from weft.solvers import (
    BASE_R_PACKAGES,
    CranSolver,
    PPM_BINARY_CODENAMES,
    _build_jobs_prefix,
    _conda_delta,
    _rlib_progress,
    check_layer_requirements,
    ppm_platform_url,
)


# -- conda delta (fix 1: the 25/26 wasted installs) ---------------------------

def _rec(name, version="1.0", deps=(), gh=False):
    r = {"name": name, "version": version, "deps": list(deps),
         "source": "snap", "sha256": ""}
    if gh:
        r["remote_sha"] = "abc123"
    return r


def test_conda_delta_drops_provided_closure_members():
    records = [_rec("alpha", deps=["beta", "gamma"]),
               _rec("beta"), _rec("gamma", deps=["beta"]),
               _rec("ghpkg", gh=True)]
    conda = {"r-beta": "1.9", "r-gamma": "2.0"}
    kept, satisfied = _conda_delta(records, conda, top_names=set())
    assert [r["name"] for r in kept] == ["alpha", "ghpkg"]
    assert kept[0]["deps"] == [], "dep edges to dropped members pruned"
    facts = {s["name"]: s for s in satisfied}
    assert facts["beta"]["conda"] == "r-beta"
    assert facts["beta"]["conda_version"] == "1.9"
    assert facts["beta"]["snapshot_version"] == "1.0", \
        "both versions recorded — a too-old conda dep must be diagnosable"


def test_conda_delta_keeps_top_level_and_github():
    records = [_rec("alpha"), _rec("ghpkg", gh=True)]
    conda = {"r-alpha": "1.0", "r-ghpkg": "9.9"}
    kept, satisfied = _conda_delta(records, conda, top_names={"alpha"})
    assert [r["name"] for r in kept] == ["alpha", "ghpkg"], \
        "explicit asks stay in the layer even when conda has the name"
    assert satisfied == []


def test_conda_delta_no_conda_is_identity():
    records = [_rec("alpha")]
    kept, satisfied = _conda_delta(records, None, set())
    assert kept == records and satisfied == []


def test_static_base_list_is_base_only():
    # recommended packages (Matrix, MASS, ...) are REAL deps and must
    # flow through the closure/delta — only true base R is excluded
    assert "base" in BASE_R_PACKAGES and "utils" in BASE_R_PACKAGES
    for rec_pkg in ("Matrix", "MASS", "lattice", "survival"):
        assert rec_pkg not in BASE_R_PACKAGES


# -- PPM platform URL (fix 2): one owner, conformance table -------------------

PPM_CASES = [
    # (url, os_id, codename, expected)
    ("https://packagemanager.posit.co/cran/2026-08-01",
     "ubuntu", "jammy",
     "https://packagemanager.posit.co/cran/__linux__/jammy/2026-08-01"),
    # old locks carry the focal segment: REWRITTEN, not preserved
    ("https://packagemanager.posit.co/cran/__linux__/focal/2026-08-01",
     "ubuntu", "noble",
     "https://packagemanager.posit.co/cran/__linux__/noble/2026-08-01"),
    # mac / unknown distro -> plain source URL
    ("https://packagemanager.posit.co/cran/__linux__/focal/2026-08-01",
     "none", "none",
     "https://packagemanager.posit.co/cran/2026-08-01"),
    ("https://packagemanager.posit.co/cran/2026-08-01",
     "darwin", "",
     "https://packagemanager.posit.co/cran/2026-08-01"),
    # unsupported codename -> plain (an unknown segment 404s the repo)
    ("https://packagemanager.posit.co/cran/2026-08-01",
     "ubuntu", "oracular",
     "https://packagemanager.posit.co/cran/2026-08-01"),
    ("https://packagemanager.posit.co/cran/2026-08-01",
     "debian", "bookworm",
     "https://packagemanager.posit.co/cran/__linux__/bookworm/2026-08-01"),
    # non-PPM URLs pass through untouched
    ("https://cran.r-project.org", "ubuntu", "jammy",
     "https://cran.r-project.org"),
]


@pytest.mark.parametrize("url,os_id,code,want", PPM_CASES)
def test_ppm_platform_url_conformance(url, os_id, code, want):
    assert ppm_platform_url(url, os_id, code) == want


def test_ppm_lock_constant_is_platform_neutral():
    assert "__linux__" not in CranSolver.PPM, \
        "locks must not bake a distro (the old constant baked focal)"
    assert "focal" not in CranSolver.PPM
    assert PPM_BINARY_CODENAMES  # the whitelist exists and is non-empty


def test_site_ppm_url_caches_and_transforms():
    from weft.solvers import site_ppm_url

    class A:
        calls = 0

        def run_cmd(self, cmd, timeout=None):
            A.calls += 1

            class R:
                out = "ubuntu:jammy\n"
            return R()

    a = A()
    u = "https://packagemanager.posit.co/cran/2026-08-01"
    assert site_ppm_url(u, a).endswith("__linux__/jammy/2026-08-01")
    site_ppm_url(u, a)
    assert A.calls == 1, "os-release read once per adapter, then cached"
    assert site_ppm_url("https://cran.r-project.org", a) == \
        "https://cran.r-project.org"


# -- build jobs (fix 3) -------------------------------------------------------

def test_build_jobs_prefix_caps_and_exports():
    s = _build_jobs_prefix(None)
    assert "MAKEFLAGS" in s and "WEFT_BUILD_JOBS" in s and "<8?" in s
    assert "<4?" in _build_jobs_prefix(4)


def test_toolchain_prelude_carries_makeflags():
    from weft.toolchain import build_env_prelude
    prelude = build_env_prelude(None, "/tc", "/parent")
    assert "MAKEFLAGS" in prelude, \
        "the one place that knows a compile is coming sets make -j"


def test_toolchain_platform_is_required():
    import inspect

    from weft import toolchain
    for fn in (toolchain.toolchain_id, toolchain.ensure_toolchain,
               toolchain.toolchain_fingerprint):
        params = inspect.signature(fn).parameters
        assert params["platform"].default is inspect.Parameter.empty, \
            f"{fn.__name__}: the linux-64 default silently mis-built " \
            "every mac/aarch64 site (platform-sweep A1)"


# -- progress poller (fix 8) --------------------------------------------------

def test_rlib_progress_polls_and_reports():
    seen = []

    class A:
        def __init__(self):
            self.n = 0

        def run_cmd(self, cmd, timeout=None):
            self.n += 3

            class R:
                out = f"{self.n}\n"
            return R()

    poller = _rlib_progress(A(), "/rlib", total=26,
                            progress=lambda **kw: seen.append(kw))
    with poller:
        deadline = time.time() + 30
        while not seen and time.time() < deadline:
            time.sleep(0.2)
    assert seen and seen[0]["total"] == 26 and seen[0]["done"] >= 1
    assert all(kw["done"] <= 26 for kw in seen), "done never exceeds total"


def test_rlib_progress_none_callback_spawns_nothing():
    before = threading.active_count()
    with _rlib_progress(None, "/rlib", 5, None):
        assert threading.active_count() == before


# -- parser sweep fixes -------------------------------------------------------

class _CranStub:
    conda_requirements = ("r-base",)


def test_layer_check_accepts_equals_pin():
    """RED before this round: 'r-base=4.4' split on whitespace never
    matched r-base — the refusal's own hint told users to version-pin
    interpreters, and a live model dropped its pin to appease it."""

    class Spec:
        conda = ["python", "r-base=4.4"]

    check_layer_requirements(Spec(), {"cran": ["Matrix"]},
                             {"cran": _CranStub()})
    for form in (["r-base ==4.4"], ["r-base >=4.3"], ["R-Base"]):
        class S2:
            conda = form
        check_layer_requirements(S2(), {"cran": ["x"]},
                                 {"cran": _CranStub()})
    with pytest.raises(WeftError) as ei:

        class S3:
            conda = ["python"]

        check_layer_requirements(S3(), {"cran": ["x"]},
                                 {"cran": _CranStub()})
    assert ei.value.code == "env.layer_conflict"


def test_release_runtime_check_tolerates_ranges():
    """RED before: '>=4.4' was read as a version string and refused."""
    rr = {"provider": "x", "release": "2026"}
    info = {"r_version": "4.4"}

    class Spec:
        conda = ["r-base >=4.3"]

    CranSolver._check_release_runtime(rr, info, Spec())      # no raise

    class Pinned:
        conda = ["r-base =4.3"]

    with pytest.raises(WeftError) as ei:
        CranSolver._check_release_runtime(rr, info, Pinned())
    assert ei.value.code == "env.layer_conflict"

    class PinnedOk:
        conda = ["r-base ==4.4.1"]

    CranSolver._check_release_runtime(rr, info, PinnedOk())  # 4.4.x fits

    class Boundary:
        conda = ["r-base =4.41"]     # 4.41 is NOT 4.4.x

    with pytest.raises(WeftError):
        CranSolver._check_release_runtime(rr, info, Boundary())


def test_parse_cran_dep_no_space_pin():
    from weft.spec import parse_cran_dep
    assert parse_cran_dep("Matrix==1.6") == \
        {"kind": "cran", "name": "Matrix", "version": "1.6"}
    assert parse_cran_dep("Matrix ==1.6")["name"] == "Matrix"


def test_probe_cran_lane_github_and_pins(monkeypatch):
    """The probe must never LIE: a github ref on the cran lane is
    unknown (not false), and a pinned name probes by NAME."""
    import weft.probe as probe_mod
    asked = []

    def fake_cran(name):
        asked.append(name)
        return {"available": True, "spelling": name}

    monkeypatch.setitem(probe_mod._BACKENDS, "cran", fake_cran)
    out = probe_mod.probe_lanes(["Matrix==1.6", "owner/repo@main"],
                                ["cran"], namespace="r")
    assert asked == ["Matrix"], "the pin is parsed off before crandb"
    gh = out["owner/repo@main"]["cran"]
    assert gh["available"] == "unknown", \
        f"github ref must be unknown, never false: {gh}"


def test_focal_literal_gone_from_pack_path():
    import inspect

    import weft.solvers as m
    src = inspect.getsource(m.CranSolver.pack_layer)
    assert '__linux__/focal/' not in src, \
        "the literal survived; use the __linux__/[^/]+/ regex"
