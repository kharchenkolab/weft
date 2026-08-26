"""First-class CRAN/GitHub R deps: dated-snapshot solver (design-next §2)."""

import pytest

from weft.api import Weft

pytestmark = [pytest.mark.solver, pytest.mark.slow]

BASE = {"conda": ["r-base =4.4"]}


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


SNAP = {"cran_snapshot": "2026-07-01"}  # determinism across test days


def test_cran_solve_lock_and_identity(w):
    spec = {"name": "r-cran", "deps": {**BASE, "cran": ["jsonlite"]},
            "system_requirements": SNAP}
    d = w.env_ensure(spec, dry_run=True)
    assert "layers" in d, d
    assert d["layers"]["cran"]["packages"] >= 1
    assert d["env_id"].startswith("env:v2:")
    d2 = w.env_ensure(spec, dry_run=True)
    assert d2["env_id"] == d["env_id"]  # deterministic under pinned snapshot


def test_github_ref_pins_commit_sha(w):
    env = w.env_ensure({"name": "r-gh",
                        "deps": {**BASE, "cran": ["tidyverse/glue@main"]},
                        "system_requirements": SNAP})
    assert "env_id" in env, env
    rec = w.env_why(env["env_id"], "glue")
    assert rec["ecosystem"] == "cran"
    assert len(rec["record"]["remote_sha"]) == 40  # branch → exact commit
    assert rec["record"]["tarball"].endswith(rec["record"]["remote_sha"])


def test_wrong_exact_pin_names_the_fix(w):
    r = w.env_ensure({"name": "bad-pin",
                      "deps": {**BASE, "cran": ["jsonlite ==0.0.1"]},
                      "system_requirements": SNAP})
    assert r["error"] == "env.solve_conflict"
    assert "change it to ==" in r["hints"]["suggestion"]
    assert r["hints"]["snapshot"].endswith("2026-07-01")


def test_layer_conflict_without_r_base(w):
    r = w.env_ensure({"name": "no-r", "deps": {"conda": ["python =3.12"],
                                               "cran": ["jsonlite"]}})
    assert r["error"] == "env.layer_conflict"
    assert r["hints"]["needs"] == "r-base in deps.conda"


def test_github_tarball_realizes_from_source(w):
    """A github-ref package compiles from its SHA-pinned tarball (needs
    the conda layer's toolchain — glue has C code)."""
    env = w.env_ensure({"name": "r-gh-real",
                        "deps": {"conda": ["r-base =4.4", "c-compiler", "make"],
                                 "cran": ["tidyverse/glue@main"]},
                        "system_requirements": SNAP})
    assert "env_id" in env, env
    r = w.task_submit({
        "command": "Rscript -e 'cat(glue::glue(\"v-{1+1}\"))' > results/g.txt",
        "env": env["env_id"], "outputs": ["results/"], "site": "local"})
    job = w.runner.wait(r["job_id"], 1800)
    assert job["state"] == "DONE", job["error"]
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/g.txt")
    assert out["preview"]["lines"] == ["v-2"]


def test_cran_layer_realizes_and_runs(w):
    env = w.env_ensure({"name": "r-run",
                        "deps": {**BASE, "cran": ["jsonlite"]},
                        "system_requirements": SNAP})
    assert "env_id" in env, env
    r = w.task_submit({
        "command": "Rscript -e 'cat(jsonlite::toJSON(list(ok=TRUE)))' "
                   "> results/out.txt",
        "env": env["env_id"], "outputs": ["results/"], "site": "local",
    })
    job = w.runner.wait(r["job_id"], 1800)
    assert job["state"] == "DONE", job["error"]
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/out.txt")
    assert out["preview"]["lines"] == ['{"ok":[true]}']
    kinds = [e["kind"] for e in w.events_poll(0, 800, compact=False)["events"]]
    assert "realize.layer" in kinds and "realize.layer.done" in kinds


def test_subdir_ref_resolves_live():
    """The founding vocabulary split: a monorepo R package
    (dmlc/xgboost's R-package subdir) resolves against the LIVE api —
    this exact shape was a 404 misdiagnosed as an unresolvable repo."""
    from weft.solvers import CranSolver
    got = CranSolver._github_resolve("dmlc/xgboost", "HEAD", "R-package")
    assert got["name"] == "xgboost"
    assert got["subdir"] == "R-package"
    assert got["sha"]


@pytest.mark.docker
def test_user_r_library_never_enters_libpaths(sshd_site, tmp_path,
                                              pixi_bin, linux_platforms):
    """Hostile-ambient battery, the R direction (aba2 casualty 1): a
    user-level R library on the site must NOT appear on .libPaths —
    pre-umbrella it sat FIRST, and a mismatched-libR binary there
    segfaulted the env deterministically while reading as a package
    bug."""
    from weft.api import Weft
    w = Weft(tmp_path / "ws-rlib", pixi_bin=pixi_bin)
    w.register_site("beam", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.3
    env = w.env_ensure({"name": "r-herm", "platforms": linux_platforms,
                        "deps": {"conda": ["r-base"]}})
    assert "env_id" in env, env
    # forge the hostile ambient state: a user library with a marker
    adapter = w._adapter("beam")
    adapter.run_cmd("mkdir -p ~/R/library/forgedpkg && "
                    "echo x > ~/R/library/forgedpkg/DESCRIPTION")
    r = w.task_submit({
        "command": "Rscript -e 'cat(paste(.libPaths(), collapse=\"\\n\"))'"
                   " > results/libpaths.txt",
        "env": env["env_id"], "outputs": ["results/"], "site": "beam"})
    job = w.runner.wait(r["job_id"], 600)
    assert job["state"] == "DONE", job.get("error")
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/libpaths.txt")
    text = "\n".join(out["preview"]["lines"])
    assert "/R/library" not in text.replace("lib/R/library", "")  # no ~lib
    assert "weft-user-library" in text or ".pixi" in text
    w.close()
