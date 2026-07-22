"""Round H: additional + release-pinned R repositories.

The extra repo is HERMETIC — a generated file:// CRAN-like repo whose
package depends on a base-mirror package — so joint cross-repo closure is
proven without adding a third-party service to the suite."""

import subprocess
import tarfile

import pytest

from weft.api import Weft
from weft.solvers import RELEASE_REPO_PROVIDERS, register_release_repo_provider

pytestmark = [pytest.mark.solver, pytest.mark.slow]

SNAP = {"cran_snapshot": "2026-07-01"}


def _make_local_repo(root, name="weftdemo", version="0.1.0",
                     depends="jsonlite") -> str:
    """A minimal CRAN-like source repo: src/contrib/PACKAGES + one
    package whose closure spans the BASE mirror (Depends: jsonlite)."""
    pkg = root / "pkgsrc" / name
    (pkg / "R").mkdir(parents=True)
    (pkg / "DESCRIPTION").write_text(
        f"Package: {name}\nVersion: {version}\nTitle: Weft demo\n"
        f"Description: Test package.\nAuthor: weft\nMaintainer: w <w@w.w>\n"
        f"License: MIT + file LICENSE\nImports: {depends}\n")
    (pkg / "LICENSE").write_text("YEAR: 2026\nCOPYRIGHT HOLDER: weft\n")
    (pkg / "NAMESPACE").write_text(
        "export(demo_tag)\nimportFrom(jsonlite, toJSON)\n")
    (pkg / "R" / "demo.R").write_text(
        'demo_tag <- function() as.character('
        'jsonlite::toJSON("weft-extra-repo"))\n')
    contrib = root / "repo" / "src" / "contrib"
    contrib.mkdir(parents=True)
    tarball = contrib / f"{name}_{version}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(pkg, arcname=name)
    (contrib / "PACKAGES").write_text(
        f"Package: {name}\nVersion: {version}\nImports: {depends}\n"
        f"NeedsCompilation: no\n\n")
    return f"file://{root / 'repo'}"


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def test_extra_repo_joint_closure(w, tmp_path):
    """weftdemo lives ONLY in the extra repo; its dependency (jsonlite)
    only in the base snapshot — one joint resolution covers both."""
    repo = _make_local_repo(tmp_path)
    spec = {"name": "with-extra",
            "deps": {"conda": ["r-base =4.4"], "cran": ["weftdemo"]},
            "r_repositories": [repo],
            "system_requirements": SNAP}
    env = w.env_ensure(spec)
    assert "env_id" in env, env
    recs = {r["name"]: r for r in
            w.store.get_env(env["env_id"])["canonical"]["layers"]["cran"]
            ["records"]}
    assert recs["weftdemo"]["source"].startswith("file://")
    assert "packagemanager" in recs["jsonlite"]["source"]

    j = w.runner.wait(w.task_submit({
        "command": "Rscript -e 'cat(weftdemo::demo_tag())' > results/o.txt",
        "env": env["env_id"], "outputs": ["results/"],
        "site": "local"})["job_id"], 3600)
    assert j["state"] == "DONE", j["error"]
    out = next(o for o in j["manifest"]["outputs"]
               if o["path"] == "results/o.txt")
    assert "weft-extra-repo" in out["preview"]["lines"][0]

    # identity: the repo set is part of the EnvID...
    assert w.env_ensure(spec)["env_id"] == env["env_id"]      # stable
    # ...and WITHOUT the repo the same deps cannot resolve
    r = w.env_ensure({"name": "without",
                      "deps": {"conda": ["r-base =4.4"],
                               "cran": ["weftdemo"]},
                      "system_requirements": SNAP})
    assert r["error"] == "env.solve_conflict"
    assert "r_repositories" in r["hints"]["suggestion"]


def test_release_provider_expands_pins_and_validates_runtime(w, tmp_path):
    repo = _make_local_repo(tmp_path)
    register_release_repo_provider(
        "curated", lambda release: {
            "repos": [repo] if release == "1.0" else [],
            "r_version": "4.4"})
    try:
        spec = {"name": "rel",
                "deps": {"conda": ["r-base =4.4"], "cran": ["weftdemo"]},
                "r_release_repos": [{"provider": "curated",
                                     "release": "1.0"}],
                "system_requirements": SNAP}
        env = w.env_ensure(spec)
        assert "env_id" in env, env
        layer = w.store.get_env(env["env_id"])["canonical"]["layers"]["cran"]
        assert layer["releases"][0]["release"] == "1.0"
        assert repo in layer["repos"]

        # runtime validation: the release line requires R 4.4
        bad = w.env_ensure({"name": "rel-bad",
                            "deps": {"conda": ["r-base =4.3"],
                                     "cran": ["weftdemo"]},
                            "r_release_repos": [{"provider": "curated",
                                                 "release": "1.0"}],
                            "system_requirements": SNAP})
        assert bad["error"] == "env.layer_conflict"
        assert bad["hints"]["requires_r"] == "4.4"
        assert bad["hints"]["have_r"].startswith("4.3")

        # unknown provider fails fast, naming what IS registered
        r = w.env_ensure({"name": "nope", "deps": {"cran": ["x"],
                                                   "conda": ["r-base"]},
                          "r_release_repos": [{"provider": "ghost",
                                               "release": "9"}]})
        assert r["error"] == "task.invalid"
        assert "curated" in r["hints"]["registered"]
    finally:
        RELEASE_REPO_PROVIDERS.pop("curated", None)


def test_release_id_is_identity(w, tmp_path):
    """Two release lines with the SAME package set are different envs:
    the release id is part of what was asked for."""
    repo = _make_local_repo(tmp_path)
    register_release_repo_provider(
        "curated2", lambda release: {"repos": [repo], "r_version": None})
    try:
        base = {"deps": {"conda": ["r-base =4.4"], "cran": ["weftdemo"]},
                "system_requirements": SNAP}
        a = w.env_ensure({"name": "r1", **base,
                          "r_release_repos": [{"provider": "curated2",
                                               "release": "1.0"}]})
        b = w.env_ensure({"name": "r2", **base,
                          "r_release_repos": [{"provider": "curated2",
                                               "release": "2.0"}]})
        assert "env_id" in a and "env_id" in b
        assert a["env_id"] != b["env_id"]
    finally:
        RELEASE_REPO_PROVIDERS.pop("curated2", None)


def test_extends_env_child_inherits_the_repo_universe(w, tmp_path):
    """An extends_env child re-solves against the SAME repos (else its
    inherited pins could not resolve) and stays overlay-eligible."""
    repo = _make_local_repo(tmp_path)
    parent = w.env_ensure({
        "name": "p", "deps": {"conda": ["r-base =4.4", "python =3.12",
                                        "pip"],
                              "cran": ["weftdemo"]},
        "r_repositories": [repo], "system_requirements": SNAP})
    assert "env_id" in parent, parent
    child = w.env_ensure({"name": "c", "extends_env": parent["env_id"],
                          "deps": {"pypi": ["emcee"]}})
    assert "env_id" in child, child
    assert child["delta"]["layerable"] is True, child["delta"]
    layer = w.store.get_env(child["env_id"])["canonical"]["layers"]["cran"]
    assert repo in layer["repos"]
    crecs = {r["name"]: r["version"] for r in layer["records"]}
    precs = {r["name"]: r["version"] for r in
             w.store.get_env(parent["env_id"])["canonical"]["layers"]
             ["cran"]["records"]}
    assert crecs == precs         # identical layer: no drift


def test_packed_offline_with_extra_repo(w, tmp_path, pixi_bin):
    """pack_layer downloads each package from the repo that SERVED it —
    the file:// repo's tarball travels in the blob like any other."""
    from pathlib import Path
    if not (Path(pixi_bin).parent / "pixi-pack").exists():
        pytest.skip("pixi-pack not installed")
    repo = _make_local_repo(tmp_path)
    w.register_site("dark", "local", {
        "root": str(tmp_path / "site-dark"), "pixi_source": pixi_bin,
        "capabilities_override": {"internet": False,
                                  "runtimes": {"apptainer": "",
                                               "docker": False}},
    })
    env = w.env_ensure({
        "name": "dark-extra",
        "deps": {"conda": ["r-base =4.4", "c-compiler", "make"],
                 "cran": ["weftdemo"]},
        "r_repositories": [repo], "system_requirements": SNAP})
    assert "env_id" in env, env
    j = w.runner.wait(w.task_submit({
        "command": "Rscript -e 'cat(weftdemo::demo_tag())' > results/o.txt",
        "env": env["env_id"], "outputs": ["results/"],
        "site": "dark"})["job_id"], 3600)
    assert j["state"] == "DONE", j["error"]


def test_ranked_ensure_with_extra_repo_end_to_end(w, tmp_path):
    """aba check-in item 2, the real thing: the RANKED verb +
    cran_repos installs a package that exists ONLY in the secondary
    repo, verify-in-loop proves it in the composed runtime, and the
    attempt records the repositories used (provenance, like
    spelling)."""
    repo = _make_local_repo(tmp_path)
    s = w.session_start({"name": "ranked-repos",
                         "deps": {"conda": ["r-base =4.4",
                                            "r-jsonlite"]},
                         "system_requirements": SNAP}, "local")
    assert "error" not in s, s
    sid = s["session_id"]
    out = w.ensure_available({"session": sid}, ["weftdemo"],
                             lanes=["cran"], cran_repos=[repo])
    assert out["satisfied"] is True, out
    att = next(a for a in out["attempts"] if a["lane"] == "cran")
    assert att["repositories"] == [repo]
    assert att["outcome"] == "installed"
    assert out["verified"]["weftdemo"]["status"] == "passed", out
    r = w.session_exec(
        sid, "Rscript -e 'library(weftdemo); cat(demo_tag())'")
    assert r["rc"] == 0 and "weft-extra-repo" in r["stdout"], r
    w.session_stop(sid)
