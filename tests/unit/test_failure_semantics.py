"""Round C (2026-07 sweep + the four-issue field note): failure payloads
are contracts. Every raise says WHICH failure happened (dead index /
not-in-repo / broken build are different levers), every rc says WHOSE rc
it is, and codes come from the registry's MEANING."""

import datetime
import urllib.error
from types import SimpleNamespace

import pytest

from weft.adapters.base import ShimResult
from weft.errors import WeftError
from weft.session import SessionManager, _pip_failure, _r_install_failure


# ── classifiers: three failures, three levers ──────────────────────────────

def test_r_failure_dead_index_is_infrastructure():
    code, retryable, why = _r_install_failure(
        "Warning: unable to access index for repository "
        "https://cloud.r-project.org/src/contrib")
    assert (code, retryable) == ("env.solve_failed", True)
    assert "unreachable" in why


def test_r_failure_absent_package_is_a_spec_problem():
    code, retryable, _ = _r_install_failure(
        "Warning message: package 'nOtAPkg' is not available for this "
        "version of R")
    assert (code, retryable) == ("env.solve_conflict", False)


def test_r_failure_default_is_a_build_failure():
    code, retryable, _ = _r_install_failure(
        "ERROR: compilation failed for package 'xml2'\n"
        "* removing '/lib/xml2'")
    assert (code, retryable) == ("env.realize_failed", False)


def test_pip_failure_three_ways():
    assert _pip_failure("ERROR: ResolutionImpossible: for help visit ...") \
        == ("env.solve_conflict", False)
    assert _pip_failure("WARNING: Could not fetch URL https://pypi.org/...:"
                        " connection error") == ("env.solve_failed", True)
    assert _pip_failure("error: subprocess-exited-with-error gcc failed") \
        == ("env.realize_failed", False)
    # phase A's default: an unrecognized resolver crash is solver
    # INFRASTRUCTURE, not proof the spec is unsatisfiable
    assert _pip_failure("Traceback ... KeyError", default="env.solve_failed",
                        default_retryable=True) == ("env.solve_failed", True)


# ── _materialize_rlib wiring: discriminating fields per trigger path ───────

class _RAd:
    name = "fake"
    pixi_bin = "/bin/pixi"

    def __init__(self, answers):
        self.answers = list(answers)
        self.scripts = []

    def path(self, rel):
        return f"/site/{rel}"

    def run_activated(self, script, timeout=120.0):
        self.scripts.append(script)
        return self.answers.pop(0)


def _rlib_call(tmp_path, monkeypatch, answers, cran=("praise",)):
    import weft.toolchain as toolchain
    from weft.store import Store
    monkeypatch.setattr(toolchain, "ensure_toolchain",
                        lambda *a, **k: None)
    monkeypatch.setattr(SessionManager, "_stack_activation",
                        lambda self, s, a: ("true", False))
    sm = SessionManager(Store(tmp_path / "s.db"), envman=None)
    ad = _RAd(answers)
    s = {"session_id": "s1", "location": "sessions/s1"}
    return sm, ad, lambda: sm._materialize_rlib(s, ad, list(cran))


def test_rlib_dead_index_names_the_network_not_the_package(
        tmp_path, monkeypatch):
    sm, ad, call = _rlib_call(tmp_path, monkeypatch, [ShimResult(
        1, "Warning: unable to access index for repository "
           "https://cloud.r-project.org/src/contrib\n"
           "package 'praise' is not available\n", "")])
    with pytest.raises(WeftError) as ei:
        call()
    assert ei.value.code == "env.solve_failed" and ei.value.retryable
    assert ei.value.hints["install_rc"] == 1
    assert ei.value.hints["verify_rc"] == 0
    # the install script pins the C locale the classifier depends on
    assert "LC_ALL=C" in ad.scripts[0]


def test_rlib_verify_failure_says_whose_rc_and_what_is_missing(
        tmp_path, monkeypatch):
    """install rc 0, verify rc 1 — the OLD payload said {"rc": 0} inside
    a raised failure (the field agent hunted a phantom)."""
    sm, ad, call = _rlib_call(tmp_path, monkeypatch, [
        ShimResult(0, "ok\n", ""),
        ShimResult(1, "MISSING: praise \n", "")])
    with pytest.raises(WeftError) as ei:
        call()
    assert ei.value.code == "env.realize_failed"
    assert ei.value.hints["install_rc"] == 0
    assert ei.value.hints["verify_rc"] == 1
    assert ei.value.hints["missing"].startswith("MISSING:")


def test_rlib_absent_package_is_solve_conflict(tmp_path, monkeypatch):
    sm, ad, call = _rlib_call(tmp_path, monkeypatch, [ShimResult(
        1, "Warning message: package 'praise' is not available for this "
           "version of R\n", "")])
    with pytest.raises(WeftError) as ei:
        call()
    assert ei.value.code == "env.solve_conflict"
    assert not ei.value.retryable


# ── github resolve: 404 is a spec problem, the rest is weather ─────────────

def _fake_urlopen(exc):
    def opener(req, timeout=30):
        raise exc
    return opener


def test_github_404_is_solve_conflict(monkeypatch):
    from weft.solvers import CranSolver
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(
        urllib.error.HTTPError("u", 404, "Not Found", {}, None)))
    with pytest.raises(WeftError) as ei:
        CranSolver._github_resolve("owner/absent", "HEAD")
    assert ei.value.code == "env.solve_conflict"
    assert not ei.value.retryable
    assert ei.value.hints["http_status"] == 404


def test_github_rate_limit_is_retryable_infrastructure(monkeypatch):
    from weft.solvers import CranSolver
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(
        urllib.error.HTTPError("u", 403, "rate limit exceeded", {}, None)))
    with pytest.raises(WeftError) as ei:
        CranSolver._github_resolve("r-lib/crayon", "HEAD")
    assert ei.value.code == "env.solve_failed" and ei.value.retryable
    assert ei.value.hints["http_status"] == 403


def test_github_network_outage_is_retryable(monkeypatch):
    from weft.solvers import CranSolver
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(
        urllib.error.URLError("connection refused")))
    with pytest.raises(WeftError) as ei:
        CranSolver._github_resolve("r-lib/crayon", "HEAD")
    assert ei.value.code == "env.solve_failed" and ei.value.retryable


# ── snapshot default: UTC-derived, concrete, never local-today ─────────────

def test_cran_snapshot_default_is_utc_minus_two():
    """A controller ahead of UTC (or of the mirror's publishing lag)
    asking for local-today hit a snapshot that does not exist yet —
    every unpinned solve failed in a nightly dead window (note #4)."""
    from weft.solvers import CranSolver
    solver = CranSolver.__new__(CranSolver)
    repos, snapshot, releases = solver._repo_set(
        SimpleNamespace(system_requirements={}))
    want = (datetime.datetime.now(datetime.timezone.utc).date()
            - datetime.timedelta(days=2)).isoformat()
    assert want in snapshot
    assert repos == [snapshot] and releases == []


def test_cran_snapshot_explicit_date_wins():
    from weft.solvers import CranSolver
    solver = CranSolver.__new__(CranSolver)
    _, snapshot, _ = solver._repo_set(
        SimpleNamespace(system_requirements={"cran_snapshot": "2026-07-01"}))
    assert "2026-07-01" in snapshot


# ── adapter recodes: never-started and refused-write say so ────────────────

def test_submit_failure_is_sched_rejected_not_user_code(tmp_path):
    from weft.adapters.local import LocalAdapter
    from weft.adapters.ssh import SSHAdapter
    for ad in (LocalAdapter("l", tmp_path / "A"),
               SSHAdapter("s", "unused", str(tmp_path / "B"),
                          transport="local")):
        ad.shim = lambda *a, **k: ShimResult(1, "", "run: no cmd.sh")
        with pytest.raises(WeftError) as ei:
            ad.submit("jobs/j1", {})
        assert ei.value.code == "sched.rejected"
        assert ei.value.stage == "submit"


def test_write_file_refused_is_not_an_outage(tmp_path):
    from weft.adapters.ssh import SSHAdapter
    root = tmp_path / "B"
    (root / "ro").mkdir(parents=True)
    (root / "ro").chmod(0o555)
    ad = SSHAdapter("s", "unused", str(root), transport="local")
    try:
        with pytest.raises(WeftError) as ei:
            ad.write_file("ro/f.txt", b"x")
        assert ei.value.code == "data.transfer_failed"
        assert not ei.value.retryable          # backoff cannot free disk
        assert "permissions" in ei.value.hints["suggestion"]
    finally:
        (root / "ro").chmod(0o755)


# ── kernel / retain / publish recodes ──────────────────────────────────────

def test_stopped_kernel_is_not_a_node_failure():
    from weft.kernel import KernelManager
    with pytest.raises(WeftError) as ei:
        KernelManager._assert_alive(None, {"kernel_id": "k1",
                                           "state": "stopped"})
    assert ei.value.code == "task.invalid"
    assert ei.value.hints["state"] == "stopped"
    assert "kernel_restart" in ei.value.hints["suggestion"]


def test_unregistered_site_is_not_an_outage():
    from weft.retain import RetainManager
    rm = RetainManager.__new__(RetainManager)
    rm.adapters = {}
    rm._target_row = lambda t: ("job", {"site": "ghost"}, "jobs/j1")
    with pytest.raises(WeftError) as ei:
        rm._sandbox_path("jb_x", "o.txt")
    assert ei.value.code == "task.invalid"
    assert "register_site" in ei.value.hints["suggestion"]


def test_retain_headroom_is_storage_pressure(tmp_path):
    from weft.retain import RetainManager
    rm = RetainManager.__new__(RetainManager)
    with pytest.raises(WeftError) as ei:
        rm._check_budgets(total=1e9, nfiles=3, max_gb=None,
                          headroom_gb=10**9, location=str(tmp_path / "x"),
                          in_place=False)
    assert ei.value.code == "quota.storage"
    assert "free_gb" in ei.value.hints
    # the caller-set cap stays a task problem
    with pytest.raises(WeftError) as ei2:
        rm._check_budgets(total=5e9, nfiles=3, max_gb=1, headroom_gb=None,
                          location=str(tmp_path / "x"), in_place=True)
    assert ei2.value.code == "task.invalid"


def test_corrupt_catalog_is_damaged_content_not_contention():
    from weft.publish import _read_catalog

    class _Ad:
        def read_file(self, rel, max_bytes=None):
            return b"{ not json"

    with pytest.raises(WeftError) as ei:
        _read_catalog(_Ad(), "/tree")
    assert ei.value.code == "data.verify_failed"
    assert "never repairs" in ei.value.hints["suggestion"]


# ── collect ingest: transfer problem, not corrupt outputs ──────────────────

def test_ingest_failure_is_transfer_failed(tmp_path):
    from weft.cas import LocalCAS
    from weft.data import DataManager
    from weft.store import Store
    from weft.task import Task

    class _Ad:
        name = "fake"

        def path(self, rel):
            return f"/x/{rel}"

        def shim(self, args, timeout=60.0):
            if args[0] == "hash-tree":
                return ShimResult(0, f"file\t.\t0\t3\t{'d' * 64}\n", "")
            if args[0] == "ingest":
                return ShimResult(1, "", "ln: cross-device link failed")
            return ShimResult(0, "", "")

        def write_file(self, rel, data, mode=None):
            pass

        def transfer_endpoint(self):
            return {"method": "local-link", "cas_root": "/x/cas"}

    dm = DataManager(Store(tmp_path / "s.db"), LocalCAS(tmp_path / "cas"),
                     tmp_path)
    with pytest.raises(WeftError) as ei:
        dm.collect_outputs(_Ad(), "jobs/j1",
                           Task.from_dict({"command": "true",
                                           "outputs": ["o.txt"]}))
    assert ei.value.code == "data.transfer_failed" and ei.value.retryable
