"""Environment reuse across workspaces + userspace compilation.

Reuse: a second workspace (fresh store) pointing at the same site must
re-adopt existing realizations from the site marker instead of rebuilding —
the site's `envs/` cache is the shared memory of what is installed where.

Toolchain: the fixture image has no compiler. A conda-forge toolchain env
realizes entirely in userspace; a task compiles a C++ source tree (shipped
as a DataRef); a downstream task runs the binary via site-side chaining;
recompilation memoizes.
"""

import pytest

from weft.api import Weft

pytestmark = [pytest.mark.docker, pytest.mark.solver]

MC_PI = r"""
#include <cstdio>
#include <cstdlib>
int main(int argc, char** argv) {
    long n = argc > 1 ? atol(argv[1]) : 100000;
    unsigned seed = 12345;
    long in = 0;
    for (long i = 0; i < n; i++) {
        double x = (double)rand_r(&seed) / RAND_MAX;
        double y = (double)rand_r(&seed) / RAND_MAX;
        if (x * x + y * y <= 1.0) in++;
    }
    printf("%.4f\n", 4.0 * in / n);
    return 0;
}
"""


def _mk(tmp_path, pixi_bin, sshd_site, sub, root=None):
    w = Weft(tmp_path / sub, pixi_bin=pixi_bin)
    w.register_site("beamlab", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": root or sshd_site["root"], "pixi_source": pixi_bin,
    })
    return w


TINY = {"name": "reuse-tiny", "deps": {"conda": ["xz >=5"]}}


def test_cross_workspace_realization_readoption(tmp_path, pixi_bin, sshd_site,
                                                linux_platforms):
    w1 = _mk(tmp_path, pixi_bin, sshd_site, "ws1")
    tiny = {**TINY, "platforms": linux_platforms}
    env1 = w1.env_ensure(tiny)["env_id"]
    r1 = w1.task_submit({"command": "xz --version > results/v.txt", "env": env1,
                         "outputs": ["results/"], "site": "beamlab"})
    assert w1.runner.wait(r1["job_id"], 600)["state"] == "DONE"
    marker_rel = f"envs/{env1.split(':')[-1]}/.weft-ready"
    mtime1 = w1.adapters["beamlab"].run_cmd(
        f"stat -c %Y $WEFT_ROOT/{marker_rel}").out.strip()

    # a different project on the same laptop, same site: fresh store,
    # re-solves the same spec to the same EnvID, re-adopts the realization
    w2 = _mk(tmp_path, pixi_bin, sshd_site, "ws2")
    env2 = w2.env_ensure(tiny)["env_id"]
    assert env2 == env1  # deterministic solve from cached repodata
    r2 = w2.task_submit({"command": "xz --help > results/h.txt", "env": env2,
                         "outputs": ["results/"], "site": "beamlab"})
    job2 = w2.runner.wait(r2["job_id"], 300)
    assert job2["state"] == "DONE", job2["error"]
    mtime2 = w2.adapters["beamlab"].run_cmd(
        f"stat -c %Y $WEFT_ROOT/{marker_rel}").out.strip()
    assert mtime2 == mtime1  # marker untouched: adopted, not rebuilt
    real = w2.store.get_realization(env1, "beamlab")
    assert real["state"] == "ready"


def test_equivalent_specs_share_envid(tmp_path, pixi_bin, sshd_site,
                                      linux_platforms):
    w = _mk(tmp_path, pixi_bin, sshd_site, "ws3")
    a = w.env_ensure({"name": "a", "platforms": linux_platforms,
                      "deps": {"conda": ["xz >=5", "zlib"]}})
    b = w.env_ensure({"name": "b-different-label", "platforms": linux_platforms,
                      "deps": {"conda": ["zlib", "xz >=5"]}})  # order differs
    assert a["env_id"] == b["env_id"]


def test_userspace_toolchain_compile_and_chain(tmp_path, pixi_bin, sshd_site,
                                               linux_platforms):
    w = _mk(tmp_path, pixi_bin, sshd_site, "ws4")
    # the site image ships no compiler at all
    chk = w.site_exec("beamlab", "command -v g++ cc gcc || echo NO-COMPILER",
                      why="verify site has no system toolchain")
    assert "NO-COMPILER" in chk["stdout"]

    src = tmp_path / "ws4" / "mc-pi"
    src.mkdir(parents=True)
    (src / "main.cpp").write_text(MC_PI)
    src_ref = w.data_register("mc-pi")["ref"]

    toolchain = w.env_ensure({
        "name": "cxx-toolchain",
        "platforms": linux_platforms,
        "deps": {"conda": ["cxx-compiler", "make"]},
    })
    assert "env_id" in toolchain, toolchain

    compile_task = {
        "command": "${CXX} -O2 src/main.cpp -o build/mc_pi && echo ok > build/status.txt",
        "env": toolchain["env_id"],
        "inputs": [{"ref": src_ref, "mount_as": "src"}],
        "outputs": ["build/"],
        "site": "beamlab",
    }
    r = w.task_submit(compile_task)
    assert "job_id" in r, r
    job = w.runner.wait(r["job_id"], 900)
    assert job["state"] == "DONE", job["error"]
    binary = next(o for o in job["manifest"]["outputs"]
                  if o["path"] == "build/mc_pi")
    assert binary["bytes"] > 1000

    # identical source + toolchain + command => memoized, no rebuild
    r_again = w.task_submit(compile_task)
    assert r_again.get("memoized") is True

    # downstream task runs the built binary — 0 bytes staged (chaining)
    r2 = w.task_submit({
        "command": "./bin/mc_pi 200000 > results/pi.txt",
        "env": None,   # plain binary, no env needed
        "inputs": [{"ref": binary["ref"], "mount_as": "bin/mc_pi"}],
        "outputs": ["results/"],
        "site": "beamlab",
    })
    assert r2["plan"]["staging"]["bytes_to_move"] == 0
    job2 = w.runner.wait(r2["job_id"], 300)
    assert job2["state"] == "DONE", job2["error"]
    pi = next(o for o in job2["manifest"]["outputs"]
              if o["path"] == "results/pi.txt")
    val = float(pi["preview"]["lines"][0])
    assert 3.0 < val < 3.3  # deterministic seed; sanity band

    # the realization record is the per-site memory of the toolchain
    real = w.env_status(toolchain["env_id"])["realizations"]
    assert any(x["site"] == "beamlab" and x["state"] == "ready" for x in real)
