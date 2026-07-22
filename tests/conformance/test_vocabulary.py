"""Vocabulary conformance: one grammar, every consumer, AGREEMENT
asserted. A string one lane accepts must not be mangled, silently
reinterpreted, or misdiagnosed by another (2026-07 vocabulary sweep:
subdir refs installed in sessions and 404'd in solves; same-owner refs
collapsed in merges; soft pins leaked into pip; three walltime
grammars)."""

import pytest

from weft.errors import WeftError
from weft.spec import (EnvSpec, _dep_key, _merge_deps, parse_cran_dep,
                       refuse_duplicate_deps)

# ── the cran shape table: THE vocabulary, enumerated ───────────────────────

CRAN_ACCEPTED = [
    ("praise", {"kind": "cran", "name": "praise", "version": None}),
    ("jsonlite ==2.0.1", {"kind": "cran", "name": "jsonlite",
                          "version": "2.0.1"}),
    ("owner/repo", {"kind": "github", "repo": "owner/repo",
                    "subdir": None, "ref": "HEAD"}),
    ("owner/repo@v1", {"kind": "github", "repo": "owner/repo",
                       "subdir": None, "ref": "v1"}),
    ("owner/repo/rpkg@v1", {"kind": "github", "repo": "owner/repo",
                            "subdir": "rpkg", "ref": "v1"}),
    ("owner/repo/a/b@v1", {"kind": "github", "repo": "owner/repo",
                           "subdir": "a/b", "ref": "v1"}),
    # a '/' AFTER the '@' is a branch name, never a subdir
    ("owner/repo@feat/x", {"kind": "github", "repo": "owner/repo",
                           "subdir": None, "ref": "feat/x"}),
]
CRAN_REFUSED = ["pkg >=1.2", "pkg <2", "owner/@v1", "/x@v1", "a b/c@v1"]


def test_cran_grammar_table():
    for dep, want in CRAN_ACCEPTED:
        assert parse_cran_dep(dep) == want, dep
    for dep in CRAN_REFUSED:
        with pytest.raises(WeftError) as ei:
            parse_cran_dep(dep)
        assert ei.value.code == "task.invalid", dep


def test_solver_parse_is_the_same_grammar():
    from weft.solvers import CranSolver
    for dep, want in CRAN_ACCEPTED:
        assert CranSolver._parse(dep) == want, dep


def test_session_lane_refuses_what_the_solver_refuses(tmp_path,
                                                      monkeypatch):
    """The session lane used to swallow any operator by reducing to the
    bare name — snapshot then re-emitted a string the solve lane
    refuses: a working session, unsnapshottable."""
    from weft.session import SessionManager
    from weft.store import Store
    import weft.toolchain as toolchain
    monkeypatch.setattr(toolchain, "ensure_toolchain", lambda *a, **k: None)
    monkeypatch.setattr(SessionManager, "_stack_activation",
                        lambda self, s, a: ("true", False))

    class _Ad:
        name = "fake"
        pixi_bin = "/bin/pixi"
        calls = 0

        def path(self, rel):
            return f"/site/{rel}"

        def run_activated(self, script, timeout=120.0):
            self.calls += 1
            raise AssertionError("must refuse before running anything")

    sm = SessionManager(Store(tmp_path / "s.db"), envman=None)
    ad = _Ad()
    with pytest.raises(WeftError) as ei:
        sm._materialize_rlib({"session_id": "s1",
                              "location": "sessions/s1"}, ad,
                             ["forecast >=8.0"])
    assert ei.value.code == "task.invalid" and ad.calls == 0


def test_emitted_vocabulary_is_consumable():
    """Round-trip at the parse level: every shape the vocabulary accepts
    must survive spec intake AND the solver's parse — the hand-off that
    broke (session -> snapshot -> solve)."""
    from weft.solvers import CranSolver
    for dep, _ in CRAN_ACCEPTED:
        s = EnvSpec.from_dict({"deps": {"conda": ["r-base"],
                                        "cran": [dep]}})
        assert s.deps_extra["cran"] == [dep]
        CranSolver._parse(dep)          # must not raise


# ── merges: same vocabulary, same keys, no collapse ────────────────────────

def test_merge_does_not_collapse_same_owner_refs():
    """split_constraint keyed every lane: its name regex stops at '/'
    and lowercases, so two same-owner refs merged to ONE — a minted
    EnvID silently lost a declared package (sweep #1)."""
    out = _merge_deps([], ["userA/pkg1@v1", "userA/pkg2@v1"], "cran")
    assert out == ["userA/pkg1@v1", "userA/pkg2@v1"]
    out2 = _merge_deps(["userA/pkgX@v9"],
                       ["userA/pkg1@v1", "userA/pkg2@v1"], "cran")
    assert out2 == ["userA/pkgX@v9", "userA/pkg1@v1", "userA/pkg2@v1"]


def test_merge_respects_case_sensitive_ecosystems():
    assert _merge_deps(["Matrix ==1"], ["matrix ==2"], "cran") == \
        ["Matrix ==1", "matrix ==2"]
    # conda stays case-insensitive (child wins)
    assert _merge_deps(["Xz =5"], ["xz =6"], "conda") == ["xz =6"]
    # pypi unifies per PEP 503
    assert _merge_deps(["foo_bar ==1"], ["foo-bar ==2"], "pypi") == \
        ["foo-bar ==2"]


def test_merge_overrides_same_source_ref():
    assert _merge_deps(["userA/pkg1@v1"], ["userA/pkg1@v2"], "cran") == \
        ["userA/pkg1@v2"]


def test_dup_key_same_source_two_refs_is_a_duplicate():
    """The same package at two refs is the same package twice."""
    with pytest.raises(WeftError):
        refuse_duplicate_deps("cran", ["userA/pkg1@v1", "userA/pkg1@v2"])
    # different subdir = different package: fine
    refuse_duplicate_deps("cran", ["o/mono/a@v1", "o/mono/b@v1"])
    assert _dep_key("cran", "o/mono/a@v1") == "o/mono/a"


# ── soft pins never reach external tools ───────────────────────────────────

def test_soft_pin_stripped_everywhere():
    from weft.session import _pixi_spec
    assert _pixi_spec("scipy ==1.14.1?") == "scipy==1.14.1"
    assert _pixi_spec("numpy >=2") == "numpy>=2"
    from weft.lock import render_pixi_manifest
    from weft.spec import current_platform
    m = render_pixi_manifest(EnvSpec(name="t",
                                     platforms=[current_platform()],
                                     conda=["scipy ==1.14.1?"]))
    assert "?" not in m


# ── walltime: one grammar, slurm's, everywhere ─────────────────────────────

WALLTIMES = [
    ("04:00:00", 14400), ("30:00", 1800), ("30", 1800),   # bare = MINUTES
    ("1-00:00:00", 86400), ("2-12", 216000),              # D-HH pads RIGHT
    ("2-12:30", 217800), ("infinite", None), ("", None),
]


def test_walltime_single_grammar():
    from weft.capability import slurm_time_to_s
    from weft.runner_util import parse_walltime, walltime_to_s
    for s, want in WALLTIMES:
        assert walltime_to_s(s) == want, s
        assert parse_walltime(s) == want, s
        assert slurm_time_to_s(s) == want, s
    with pytest.raises(WeftError) as ei:
        walltime_to_s("tomorrow")
    assert ei.value.code == "task.invalid"
    assert slurm_time_to_s("garbage-probe-token") is None  # probe data


def test_walltime_refused_at_task_intake():
    from weft.task import Task
    with pytest.raises(WeftError) as ei:
        Task.from_dict({"command": "true",
                        "resources": {"walltime": "4 hours"}})
    assert ei.value.code == "task.invalid"
    Task.from_dict({"command": "true",
                    "resources": {"walltime": "1-00:00:00"}})  # slurm-legal


# ── subdir refs: resolve + all three install sites ─────────────────────────

def test_github_resolve_reads_subdir_description(monkeypatch):
    from weft.solvers import CranSolver
    urls = []

    def fake_urlopen(req, timeout=30):
        import io
        urls.append(req.full_url)
        if "api.github.com" in req.full_url:
            return io.BytesIO(b'{"sha": "abc123"}')
        return io.BytesIO(b"Package: rpkg\nVersion: 1.0\nImports: cli\n")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    got = CranSolver._github_resolve("owner/mono", "v2", "rpkg")
    assert got["name"] == "rpkg" and got["subdir"] == "rpkg"
    assert got["deps"] == ["cli"]
    assert urls[1].endswith("/abc123/rpkg/DESCRIPTION")


def test_two_different_404s_two_different_verdicts(monkeypatch):
    import urllib.error
    from weft.solvers import CranSolver

    def dead_commits(req, timeout=30):
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", dead_commits)
    with pytest.raises(WeftError) as ei:
        CranSolver._github_resolve("owner/absent", "HEAD")
    assert ei.value.code == "env.solve_conflict"
    assert "repo or ref does not exist" in ei.value.detail

    def dead_description(req, timeout=30):
        import io
        if "api.github.com" in req.full_url:
            return io.BytesIO(b'{"sha": "abc123"}')
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", dead_description)
    with pytest.raises(WeftError) as ei2:
        CranSolver._github_resolve("owner/mono", "v2")
    assert ei2.value.code == "env.solve_conflict"
    assert "no DESCRIPTION at the repository root" in ei2.value.detail
    assert "owner/repo/subdir@ref" in ei2.value.hints["suggestion"]


def test_gh_install_snippet_handles_subdir():
    from weft.solvers import _r_gh_install
    recs = [{"name": "rpkg", "remote_sha": "abc", "subdir": "rpkg",
             "tarball": "https://x/t.tar.gz"},
            {"name": "root", "remote_sha": "def",
             "tarball": "https://x/r.tar.gz"}]
    code = _r_gh_install(recs)
    assert "untar(" in code and 'file.path(' in code
    assert '"rpkg"' in code and '""' in code       # parallel subdir vector
    assert _r_gh_install([{"name": "n", "tarball": "t"}]) == ""  # cran only


def test_subdir_tarball_repack(tmp_path):
    import io
    import tarfile
    from weft.solvers import _subdir_tarball
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for p, body in (("mono-abc/README.md", b"hi"),
                        ("mono-abc/rpkg/DESCRIPTION", b"Package: rpkg\n"),
                        ("mono-abc/rpkg/R/x.R", b"f <- 1\n")):
            ti = tarfile.TarInfo(p)
            ti.size = len(body)
            t.addfile(ti, io.BytesIO(body))
    out = _subdir_tarball(buf.getvalue(), "rpkg", "rpkg")
    names = tarfile.open(fileobj=io.BytesIO(out), mode="r:gz").getnames()
    assert "rpkg/DESCRIPTION" in names and "rpkg/R/x.R" in names
    assert not any("README" in n for n in names)
    with pytest.raises(WeftError) as ei:
        _subdir_tarball(buf.getvalue(), "nope", "x")
    assert ei.value.code == "env.realize_failed"
