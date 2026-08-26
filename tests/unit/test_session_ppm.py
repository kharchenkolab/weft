"""bug5 A3 — the session cran lane can serve PPM binaries.

Motivating incident (cbe-next prj_77a8beea): session_install(cran=…)
against a PPM-configured site printed 11/11 "installing *source*
package" and spent ~10 minutes compiling — site_ppm_url was referenced
only in solvers.py, so the platform segment that makes a PPM URL
binary never reached the session lane's repo vector. The routing is
now shared (one owner: site_ppm_url), and serving binaries pulls in
the solver lanes' load-check + source-rebuild arm (one owner:
_r_loadcheck) — a binary that installs but fails to LOAD under
conda's R must not pass the presence check."""

import re
from pathlib import Path

from helpers_verify import cold_session, no_toolchain, script_log
from weft.adapters.base import ShimResult

PPM = "https://packagemanager.posit.co/cran/2026-08-01"
LOADCHECK_MARK = "binary load failed, rebuilding from source"


def _repos_vector(script: str) -> str:
    """The `repos <- c(...)` vector — the loadcheck arm's srcrepo
    pattern ALSO contains '__linux__', so URL assertions must target
    the vector, not the whole script."""
    return re.search(r"repos <- c\(([^)]*)\)", script).group(1)


def _install_cran(tmp_path, pixi_bin, monkeypatch, repos,
                  os_release=("ubuntu", "jammy"), ppm_binaries=True):
    w, sid = cold_session(tmp_path, pixi_bin)
    no_toolchain(monkeypatch)
    ad = w.adapters["local"]
    ad._weft_os_release = os_release
    if not ppm_binaries:
        ad._weft_ppm_binaries = False
    log = script_log(monkeypatch, w, {
        "-cran-install.log": ShimResult(0, "done", ""),
        "MISSING:": ShimResult(0, "", ""),
    })
    out = w.session_install(sid, cran=["statgraphs"], cran_repos=repos)
    assert "error" not in out, out
    return [s for k, s in log if k == "-cran-install.log"][0]


def test_ppm_url_platformized_in_session_lane(tmp_path, pixi_bin,
                                              monkeypatch):
    script = _install_cran(tmp_path, pixi_bin, monkeypatch, [PPM])
    repos = _repos_vector(script)
    assert "__linux__/jammy" in repos, \
        "the binary segment is what makes a PPM URL serve binaries"
    assert PPM not in repos, "the plain form must not survive alongside"


def test_ppm_binaries_false_policy_respected(tmp_path, pixi_bin,
                                             monkeypatch):
    script = _install_cran(tmp_path, pixi_bin, monkeypatch, [PPM],
                           ppm_binaries=False)
    assert "__linux__" not in _repos_vector(script), \
        "the ppm_binaries:false lever forces plain source everywhere"


def test_unsupported_distro_stays_source(tmp_path, pixi_bin,
                                         monkeypatch):
    script = _install_cran(tmp_path, pixi_bin, monkeypatch, [PPM],
                           os_release=("rhel", "none"))
    assert "__linux__" not in _repos_vector(script), \
        "an unsupported codename in the URL 404s the whole repository"


def test_non_ppm_urls_untouched(tmp_path, pixi_bin, monkeypatch):
    script = _install_cran(tmp_path, pixi_bin, monkeypatch,
                           ["https://cran.example.org/snapshot"])
    repos = _repos_vector(script)
    assert "https://cran.example.org/snapshot" in repos
    assert "__linux__" not in repos


def test_session_lane_carries_the_loadcheck_arm(tmp_path, pixi_bin,
                                                monkeypatch):
    """Serving binaries without the load-check would ratify broken
    installs — the presence verification only proves the directory
    exists. The arm strips the __linux__ segment for the rebuild."""
    script = _install_cran(tmp_path, pixi_bin, monkeypatch, [PPM])
    assert LOADCHECK_MARK in script
    assert 'sub("__linux__/[^/]+/", "", repos)' in script
    assert 'type="source"' in script


def test_loadcheck_has_one_owner():
    """One vocabulary, one parser: the R fragment lives ONCE, in
    solvers._r_loadcheck — solver realize, solver overlay, and the
    session lane all call it. A second inline copy is the
    two-implementations bug this test pins shut. (Drift-catcher for
    the SOURCE; the behavioral pins are the session test above and
    the docker-lane solver suites.)"""
    src_dir = Path(__file__).parent.parent.parent / "src" / "weft"
    solvers = (src_dir / "solvers.py").read_text()
    session = (src_dir / "session.py").read_text()
    assert solvers.count(LOADCHECK_MARK) == 1, \
        "exactly the owner definition"
    assert session.count(LOADCHECK_MARK) == 0, \
        "the session lane calls the owner, never copies the prose"
    assert session.count("_r_loadcheck") >= 1
    assert solvers.count("loadcheck=_r_loadcheck(") == 2, \
        "both solver templates route through the owner"


def test_ppm_owner_conformance_table():
    """The one-owner URL grammar, driven at the session lane's inputs
    (same cases the solver lane pins in test_cran_lane) — both lanes
    consume the SAME function, so this asserts the shared contract
    holds for the session-shaped calls."""
    from weft.solvers import ppm_platform_url
    assert ppm_platform_url(PPM, "ubuntu", "jammy") == \
        "https://packagemanager.posit.co/cran/__linux__/jammy/2026-08-01"
    assert ppm_platform_url("https://cloud.r-project.org",
                            "ubuntu", "jammy") == \
        "https://cloud.r-project.org", "caller's non-PPM URL is law"
