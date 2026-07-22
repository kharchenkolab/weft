"""Field note #5: a manifest that does not PARSE is not an unsatisfiable
spec. Duplicates are refused at intake (task.invalid naming both
entries); a post-validation parse failure is weft's own bug
(internal.error, no pin advice); the ambiguous github-fetch text from
standalone remotes is disambiguated from the controller."""

import pytest

from weft.adapters.base import ShimResult
from weft.errors import WeftError
from weft.spec import EnvSpec, refuse_duplicate_deps

# captured verbatim from standalone remotes (2026-07-22 probe): missing
# repo and dead network produce BYTE-IDENTICAL text
GH_FETCH_FAIL = (
    "Error: Failed to install 'unknown package' from GitHub:\n"
    "  cannot open URL 'https://api.github.com/repos/org/widgetlib/"
    "contents/DESCRIPTION?ref=v2'\n")


# ── intake: duplicates refused before any solver runs ──────────────────────

def test_conda_duplicate_names_both_constraints():
    with pytest.raises(WeftError) as ei:
        EnvSpec.from_dict({"name": "x", "deps": {
            "conda": ["r-base =4.4.*", "r-irkernel", "r-base"]}})
    assert ei.value.code == "task.invalid"
    assert "'r-base'" in ei.value.detail
    assert ei.value.hints["duplicates"] == ["r-base =4.4.*", "r-base"]


def test_conda_duplicate_is_case_insensitive():
    with pytest.raises(WeftError) as ei:
        EnvSpec.from_dict({"deps": {"conda": ["Xz", "xz =5"]}})
    assert ei.value.code == "task.invalid"


def test_pypi_duplicate_normalizes_pep503():
    for pair in (["PyYAML", "pyyaml"], ["foo_bar", "foo-bar ==1.0"],
                 ["a.b.c", "a-b-c"]):
        with pytest.raises(WeftError) as ei:
            EnvSpec.from_dict({"deps": {"pypi": pair}})
        assert ei.value.code == "task.invalid", pair


def test_cran_lane_is_case_sensitive():
    """R package names genuinely differ by case — a case-insensitive
    check would refuse legitimate specs."""
    s = EnvSpec.from_dict({"deps": {"conda": ["r-base"],
                                    "cran": ["praise", "Praise"]}})
    assert s.deps_extra["cran"] == ["praise", "Praise"]


def test_cran_identical_ref_duplicate_refused():
    with pytest.raises(WeftError) as ei:
        EnvSpec.from_dict({"deps": {"conda": ["r-base"],
                                    "cran": ["r-lib/crayon@main",
                                             "r-lib/crayon@main"]}})
    assert ei.value.code == "task.invalid"


def test_variants_lane_checked_too():
    with pytest.raises(WeftError) as ei:
        EnvSpec.from_dict({"deps": {"conda": ["python"]},
                           "variants": {"linux-64": {"conda":
                                                     ["mpich", "mpich"]}}})
    assert "variants.linux-64" in ei.value.detail


def test_valid_spec_unperturbed():
    d = {"name": "ok", "deps": {"conda": ["python =3.12", "pip"],
                                "pypi": ["numpy"]}}
    s = EnvSpec.from_dict(d)
    assert s.conda == ["python =3.12", "pip"] and s.pypi == ["numpy"]
    refuse_duplicate_deps("conda", s.conda)     # idempotent on valid input


# ── lock.solve: a parse failure never wears the conflict code ──────────────

def _mini_spec():
    from weft.spec import current_platform
    return EnvSpec(name="parsecase", platforms=[current_platform()],
                   conda=["xz"])


def test_pixi_parse_failure_is_internal_error(tmp_path, pixi_bin,
                                              monkeypatch):
    """END-TO-END against real pixi: a duplicate-key manifest fails at
    PARSE (no network involved) and must come back as weft's own bug —
    with no user_pins accusation and no soft-pin advice."""
    import weft.lock as lock
    monkeypatch.setattr(
        lock, "render_pixi_manifest",
        lambda spec: '[workspace]\nname = "t"\n'
                     'channels = ["conda-forge"]\n'
                     f'platforms = ["{spec.platforms[0]}"]\n\n'
                     '[dependencies]\n"xz" = "*"\n"xz" = "5.*"\n')
    with pytest.raises(WeftError) as ei:
        lock.solve(_mini_spec(), tmp_path / "wd", pixi_bin=pixi_bin)
    assert ei.value.code == "internal.error"
    assert "duplicate key" in ei.value.hints["stderr_tail"]
    assert "user_pins" not in ei.value.hints
    assert "SOFT" not in ei.value.hints.get("suggestion", "")


def test_pixi_syntax_error_is_internal_error(tmp_path, pixi_bin,
                                             monkeypatch):
    """The generic-parse arm keys on the miette span citing pixi.toml
    line:col — messages vary per error, the span does not (probed)."""
    import weft.lock as lock
    monkeypatch.setattr(lock, "render_pixi_manifest",
                        lambda spec: "[workspace\nname = 't'\n")
    with pytest.raises(WeftError) as ei:
        lock.solve(_mini_spec(), tmp_path / "wd", pixi_bin=pixi_bin)
    assert ei.value.code == "internal.error"


# ── R github fetch: identical text, active disambiguation ──────────────────

def test_r_classifier_github_fetch_is_ambiguous_infrastructure():
    from weft.session import _r_install_failure
    code, retryable, why = _r_install_failure(GH_FETCH_FAIL)
    assert (code, retryable) == ("env.solve_failed", True)
    assert "identical" in why


def _rlib_ref_call(tmp_path, monkeypatch):
    from weft.session import SessionManager
    from weft.store import Store
    import weft.toolchain as toolchain
    monkeypatch.setattr(toolchain, "ensure_toolchain", lambda *a, **k: None)
    monkeypatch.setattr(SessionManager, "_stack_activation",
                        lambda self, s, a: ("true", False))

    class _Ad:
        name = "fake"
        pixi_bin = "/bin/pixi"

        def path(self, rel):
            return f"/site/{rel}"

        def run_activated(self, script, timeout=120.0):
            return ShimResult(1, GH_FETCH_FAIL, "")

    sm = SessionManager(Store(tmp_path / "s.db"), envman=None)
    s = {"session_id": "s1", "location": "sessions/s1"}
    return lambda: sm._materialize_rlib(s, _Ad(), ["org/widgetlib@v2"])


def test_missing_repo_becomes_a_spec_verdict(tmp_path, monkeypatch):
    from weft.solvers import CranSolver
    monkeypatch.setattr(
        CranSolver, "_github_resolve",
        staticmethod(lambda repo, ref, subdir=None: (_ for _ in ()).throw(
            WeftError("env.solve_conflict",
                      f"cannot resolve github ref {repo}@{ref}",
                      stage="solve"))))
    with pytest.raises(WeftError) as ei:
        _rlib_ref_call(tmp_path, monkeypatch)()
    assert ei.value.code == "env.solve_conflict"
    assert not ei.value.retryable
    assert ei.value.hints["bad_ref"] == "org/widgetlib@v2"
    assert ei.value.hints["checked_from_controller"] is True


def test_live_repo_blames_the_site_egress(tmp_path, monkeypatch):
    from weft.solvers import CranSolver
    monkeypatch.setattr(CranSolver, "_github_resolve",
                        staticmethod(lambda repo, ref, subdir=None: {"name": "widgetcore"}))
    with pytest.raises(WeftError) as ei:
        _rlib_ref_call(tmp_path, monkeypatch)()
    assert ei.value.code == "env.solve_failed" and ei.value.retryable
    assert "SITE" in ei.value.detail


def test_controller_offline_stays_retryable(tmp_path, monkeypatch):
    from weft.solvers import CranSolver
    monkeypatch.setattr(
        CranSolver, "_github_resolve",
        staticmethod(lambda repo, ref, subdir=None: (_ for _ in ()).throw(
            WeftError("env.solve_failed", "github unreachable",
                      stage="solve", retryable=True))))
    with pytest.raises(WeftError) as ei:
        _rlib_ref_call(tmp_path, monkeypatch)()
    assert ei.value.code == "env.solve_failed" and ei.value.retryable
    assert "controller cannot reach github" in ei.value.detail


# ── session install: intra-call duplicates refused uniformly ───────────────

def test_session_install_intra_call_duplicate(monkeypatch):
    from weft.session import SessionManager
    sm = SessionManager.__new__(SessionManager)
    monkeypatch.setattr(SessionManager, "_get",
                        lambda self, sid: {"session_id": sid})
    with pytest.raises(WeftError) as ei:
        sm.install("s1", None, cran=["praise", "praise"])
    assert ei.value.code == "task.invalid"
    assert "install.cran" in ei.value.detail


# ═══ 2026-07 injection + verdict sweeps (same round) ═══════════════════════

# ── intake guards: container-breaking strings refused ──────────────────────

def test_env_var_key_injection_refused_at_task():
    from weft.task import Task
    with pytest.raises(WeftError) as ei:
        Task.from_dict({"command": "true",
                        "env_vars": {"A=1\nrm -rf x #": "v"}})
    assert ei.value.code == "task.invalid"
    assert "shell identifier" in ei.value.detail
    t = Task.from_dict({"command": "true", "env_vars": {"OMP_NUM_THREADS":
                                                        "4"}})
    assert t.env_vars["OMP_NUM_THREADS"] == "4"


def test_env_var_key_injection_refused_at_spec():
    with pytest.raises(WeftError) as ei:
        EnvSpec.from_dict({"deps": {"conda": ["python"]},
                           "env_vars": {"B; curl u|sh": "x"}})
    assert ei.value.code == "task.invalid"


def test_platform_names_validated():
    for bad in ("linux-64]\n[evil", "a.b", 'x"y'):
        with pytest.raises(WeftError) as ei:
            EnvSpec.from_dict({"platforms": [bad],
                               "deps": {"conda": ["python"]}})
        assert ei.value.code == "task.invalid", bad
    with pytest.raises(WeftError):
        EnvSpec.from_dict({"deps": {"conda": ["python"]},
                           "variants": {"linux-64]": {"conda": ["mpich"]}}})
    s = EnvSpec.from_dict({"platforms": ["linux-aarch64", "osx-arm64"],
                           "deps": {"conda": ["python"]}})
    assert s.platforms == ["linux-aarch64", "osx-arm64"]


def test_output_paths_refuse_tab_newline():
    from weft.task import Task
    with pytest.raises(WeftError) as ei:
        Task.from_dict({"command": "true", "outputs": ["a\tb.txt"]})
    assert "tab/newline" in ei.value.detail


def test_rlib_script_json_escapes_caller_strings(tmp_path, monkeypatch):
    """Parity with solvers.py: a quote in a name/url must stay INSIDE
    the R string."""
    from weft.session import SessionManager
    from weft.store import Store
    import weft.toolchain as toolchain
    monkeypatch.setattr(toolchain, "ensure_toolchain", lambda *a, **k: None)
    monkeypatch.setattr(SessionManager, "_stack_activation",
                        lambda self, s, a: ("true", False))

    class _Ad:
        name = "fake"
        pixi_bin = "/bin/pixi"
        scripts = []

        def path(self, rel):
            return f"/site/{rel}"

        def run_activated(self, script, timeout=120.0):
            self.scripts.append(script)
            return ShimResult(1, "boom", "")

    sm = SessionManager(Store(tmp_path / "s.db"), envman=None)
    ad = _Ad()
    with pytest.raises(WeftError):
        sm._materialize_rlib({"session_id": "s1",
                              "location": "sessions/s1"}, ad,
                             ['pra"ise'], extra_repos=['https://x/"y'])
    script = ad.scripts[0]
    assert '\\"' in script                       # json escaping happened
    assert 'pra\\"ise' in script and 'https://x/\\"y' in script


# ── TSV rows: shredded row is a refusable input, not broken tooling ────────

def test_tsv_row_guard():
    from weft.data import _tsv_row
    assert _tsv_row(f"file\ta.txt\t1\t3\t{'d' * 64}", "t")[3] == 3
    for bad in ("file\ta\tb.txt\t1\t3\tdead",     # 6 fields: tab in name
                "file\tfrag\t0\t\t"):             # newline shred: int('')
        with pytest.raises(WeftError) as ei:
            _tsv_row(bad, "collection of 'o'")
        assert ei.value.code == "task.invalid"
        assert "tab or newline" in ei.value.detail


# ── session pixi add: the last catch-all, discriminated ────────────────────

def test_pixi_add_failure_four_ways():
    from weft.session import _pixi_add_failure
    code, retry, _, _ = _pixi_add_failure(
        "Error: × duplicate key: `xz`\n ╭─[/x/pixi.toml:8:2]")
    assert (code, retry) == ("internal.error", False)
    code, retry, _, stg = _pixi_add_failure(
        "Cannot solve the request because of: nothing provides Y")
    assert (code, retry, stg) == ("env.solve_conflict", False, "solve")
    code, retry, _, _ = _pixi_add_failure(
        "failed to fetch repodata from conda-forge")
    assert (code, retry) == ("env.solve_failed", True)
    code, retry, _, _ = _pixi_add_failure("gcc: error: exit status 1")
    assert (code, retry) == ("env.realize_failed", False)


# ── julia: only the resolver's own verdict is a conflict ───────────────────

def test_julia_verdict_split():
    from weft.solvers import _julia_solve_error
    e = _julia_solve_error(
        "Unsatisfiable requirements detected for package Foo", ["Foo"])
    assert e.code == "env.solve_conflict" and not e.retryable
    assert e.hints["user_pins"] == ["Foo"]
    e2 = _julia_solve_error(
        "could not download https://pkg.julialang.org/registries", ["Foo"])
    assert e2.code == "env.solve_failed" and e2.retryable
    assert "not implicated" in e2.detail


# ── bundle export: down is not gone ────────────────────────────────────────

def _bundle_fake(exc):
    class _Cas:
        def kind_of(self, ref):
            return None

    class _W:
        cas = _Cas()
        store = None

        def data_fetch(self, ref, dest):
            raise exc

    return _W()


def test_bundle_unreachable_site_is_not_evicted(tmp_path, monkeypatch):
    import weft.bundle as bundle
    monkeypatch.setattr(bundle, "_closure",
                        lambda w, j: ([], set(), {"dref:" + "e" * 64}))
    with pytest.raises(WeftError) as ei:
        bundle.export_bundle(_bundle_fake(WeftError(
            "site.unreachable", "down", stage="infra", retryable=True)),
            "jb_x", str(tmp_path / "b.tar"))
    assert ei.value.code == "site.unreachable" and ei.value.retryable
    assert "NOT evicted" in ei.value.hints["suggestion"]


def test_bundle_truly_missing_keeps_evicted_advice(tmp_path, monkeypatch):
    import weft.bundle as bundle
    monkeypatch.setattr(bundle, "_closure",
                        lambda w, j: ([], set(), {"dref:" + "e" * 64}))
    with pytest.raises(WeftError) as ei:
        bundle.export_bundle(_bundle_fake(WeftError(
            "data.missing", "no locations", stage="staging")),
            "jb_x", str(tmp_path / "b.tar"))
    assert ei.value.code == "data.missing"
    assert "evicted everywhere" in ei.value.hints["suggestion"]


# ── http fetch: a 404 is the server's final answer ─────────────────────────

def test_http_fetch_404_not_retryable(tmp_path, monkeypatch):
    import urllib.error
    from weft.sources import HttpFetcher

    def _raise(code):
        def opener(req, timeout=120):
            raise urllib.error.HTTPError("u", code, "msg", {}, None)
        return opener

    monkeypatch.setattr("urllib.request.urlopen", _raise(404))
    with pytest.raises(WeftError) as ei:
        HttpFetcher().fetch_to_file("https://x/y", tmp_path / "f")
    assert not ei.value.retryable and ei.value.hints["http_status"] == 404
    monkeypatch.setattr("urllib.request.urlopen", _raise(503))
    with pytest.raises(WeftError) as ei2:
        HttpFetcher().fetch_to_file("https://x/y", tmp_path / "f")
    assert ei2.value.retryable
