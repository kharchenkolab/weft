"""ensure_available P0: the verify oracle is honest by construction.
Fail closed on claiming, fail open on blaming: anything that prevents
a check from RUNNING is unknown — never failed, never passed."""

import pytest

from weft.errors import WeftError
from weft.verify import (MARKER, compare_versions, default_checks,
                         explicit_checks, parse_verify_output,
                         python_verify_script, r_verify_script,
                         run_verify, validate_verify)


# ── the ONE grammar ────────────────────────────────────────────────────────

def test_validator_table():
    assert validate_verify(True) == {}
    v = validate_verify({"import": ["sklearn"], "loads": ["xgboost"],
                         "versions": {"arrow": ">=15.0"}})
    assert v["versions"]["arrow"] == ">=15.0"
    for bad in ({"min_versions": {"x": "1"}},          # one key, not two
                {"versions": {"x": "<2"}},             # no upper bounds
                {"versions": {"x": "1.0"}},            # operator required
                {"import": ['x"; import os']},         # not a name
                "xgboost"):                            # not a dict
        with pytest.raises(WeftError) as ei:
            validate_verify(bad)
        assert ei.value.code == "task.invalid", bad


def test_comparator_never_passes_the_incomparable():
    assert compare_versions("2.0.3", ">=2.0") == "passed"
    assert compare_versions("1.9", ">=2.0") == "failed"
    assert compare_versions("15.0.0", "==15.0.0") == "passed"
    assert compare_versions("15.0.1", "==15.0.0") == "failed"
    assert compare_versions("1.2-3", ">=1.2.2") == "passed"   # R dashes
    assert compare_versions("2.0rc1", ">=2.0") == "unknown"   # alpha tail
    assert compare_versions("", ">=1") == "unknown"
    assert compare_versions("1.0", "~=1.0") == "unknown"      # bad spec


def test_default_checks_key_on_dist_and_resolved_names():
    """conda/pypi default is METADATA by dist name — no implicit import
    (dist != import name); cran default is library() load."""
    py = default_checks("pypi", ["scikit-learn"], {"scikit-learn": "==1.5"})
    assert py == [{"name": "scikit-learn", "kind": "metadata",
                   "want": "==1.5"}]
    r = default_checks("cran", ["xgboost"])
    assert r[0]["kind"] == "loads"


def test_explicit_checks_route_versions_by_ecosystem():
    checks = explicit_checks(
        validate_verify({"versions": {"arrow": ">=15.0", "Matrix": ">=1.6"}}),
        lane_of={"Matrix": "cran"})
    kinds = {c["name"]: c["kind"] for c in checks}
    assert kinds == {"arrow": "metadata", "Matrix": "loads"}


# ── oracle honesty: could-not-run is unknown, never a verdict ──────────────

CHECKS = [{"name": "alpha", "kind": "metadata", "want": ">=1.0"},
          {"name": "beta", "kind": "import", "want": None}]


def test_nonzero_rc_is_all_unknown():
    got = parse_verify_output(
        MARKER + '{"name": "alpha", "kind": "metadata", "ok": true, '
                 '"got": "2.0"}\n', rc=139, checks=CHECKS)
    assert all(r["status"] == "unknown" for r in got.values())
    assert "rc 139" in got["alpha"]["reason"]


def test_missing_marker_is_unknown_others_honored():
    out = MARKER + '{"name": "alpha", "kind": "metadata", "ok": true, "got": "2.0"}\n'
    got = parse_verify_output(out, rc=0, checks=CHECKS)
    assert got["alpha"]["status"] == "passed"
    assert got["beta"]["status"] == "unknown"
    assert "no verify marker" in got["beta"]["reason"]


def test_garbage_marker_is_unknown():
    out = MARKER + "not json at all\n" + MARKER + '{"no-name": 1}\n'
    got = parse_verify_output(out, rc=0, checks=[CHECKS[1]])
    assert got["beta"]["status"] == "unknown"


def test_verdict_comes_from_the_comparator_not_the_site():
    """The site reports GOT; the controller judges — a site cannot
    claim 'passed' for a versioned check."""
    out = MARKER + '{"name": "alpha", "kind": "metadata", "ok": true, "got": "0.9"}\n'
    got = parse_verify_output(out, rc=0, checks=[CHECKS[0]])
    assert got["alpha"]["status"] == "failed"
    assert got["alpha"]["got"] == "0.9" and got["alpha"]["want"] == ">=1.0"


def test_exec_exception_is_could_not_run():
    def dead(script, timeout):
        raise WeftError("site.unreachable", "ssh down", stage="infra",
                        retryable=True)
    got = run_verify(dead, "pypi", CHECKS)
    assert all(r["status"] == "unknown" for r in got.values())
    assert "site.unreachable" in got["alpha"]["reason"]


def test_check_failure_is_marker_content_not_rc():
    """A failed check must NOT exit the oracle nonzero — rc means the
    oracle broke, marker content means the check failed."""
    out = MARKER + '{"name": "beta", "kind": "import", "ok": false, ' \
                   '"reason": "No module named beta"}\n'
    got = parse_verify_output(out, rc=0, checks=[CHECKS[1]])
    assert got["beta"]["status"] == "failed"
    assert "No module named" in got["beta"]["reason"]


# ── real-interpreter smoke: the actual pipeline, this host's python ────────

class _Shim:
    def __init__(self, rc, out):
        self.rc, self.out = rc, out


def _local_exec(script, timeout):
    import subprocess
    r = subprocess.run(["sh", "-c", script], capture_output=True,
                       text=True, timeout=timeout)
    return _Shim(r.returncode, r.stdout)


def test_python_oracle_end_to_end_on_this_host():
    checks = [{"name": "pip", "kind": "metadata", "want": ">=1.0"},
              {"name": "json", "kind": "import", "want": None},
              {"name": "weft-no-such-dist", "kind": "metadata",
               "want": None}]
    got = run_verify(_local_exec, "pypi", checks)
    assert got["pip"]["status"] == "passed" and got["pip"]["got"]
    assert got["json"]["status"] == "passed"
    assert got["weft-no-such-dist"]["status"] == "failed"


def test_python_oracle_failed_import_end_to_end():
    got = run_verify(_local_exec, "pypi",
                     [{"name": "weft_absent_module", "kind": "import",
                       "want": None}])
    assert got["weft_absent_module"]["status"] == "failed"


def test_r_script_shape():
    s = r_verify_script([{"name": "xgboost", "kind": "loads",
                          "want": ">=2.0"}])
    assert 'library(nm, character.only=TRUE)' in s
    assert '"xgboost"' in s and MARKER in s
    assert r_verify_script([]) == ""


def test_python_script_never_interpolates_raw_names():
    s = python_verify_script([{"name": "scikit-learn", "kind": "metadata",
                               "want": None}])
    assert "scikit-learn" in s and "json.loads" in s


def test_conda_meta_oracle_end_to_end(tmp_path, monkeypatch):
    """conda packages that are not python dists (no importlib metadata):
    the conda-meta record is the installed fact; the digit-anchored glob
    keeps a prefix-name sibling from matching."""
    import os
    import subprocess
    from weft.verify import sh_conda_verify_script
    meta = tmp_path / "conda-meta"
    meta.mkdir()
    (meta / "xz-5.4.6-h1234_0.json").write_text("{}")
    (meta / "xz-utils-9.9-h0_0.json").write_text("{}")   # sibling
    checks = [{"name": "xz", "kind": "conda_meta", "want": ">=5.0"},
              {"name": "absent", "kind": "conda_meta", "want": None}]
    r = subprocess.run(["sh", "-c", sh_conda_verify_script(checks)],
                       capture_output=True, text=True,
                       env={**os.environ, "CONDA_PREFIX": str(tmp_path)})
    res = parse_verify_output(r.stdout, r.returncode, checks)
    assert res["xz"]["status"] == "passed" and res["xz"]["got"] == "5.4.6"
    assert res["absent"]["status"] == "failed"


def test_default_checks_conda_routes_to_conda_meta():
    assert default_checks("conda", ["cmake"])[0]["kind"] == "conda_meta"
