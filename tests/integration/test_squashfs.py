"""Squashfs realization (misc/sqaush.md option A, round 14): one mounted
image instead of a file forest on the shared FS.

Runs against the fuse-capable sshd fixture (--device /dev/fuse +
CAP_SYS_ADMIN; userns verified inside OrbStack/docker). The container's
root fs is not parallel, so squashfs is forced per site with
config prefer="squashfs" — exactly the institutional-deployment shape
(NFS-hosted read-only heavy envs) this round exists for.
"""

import pytest

from weft.api import Weft

pytestmark = [pytest.mark.docker, pytest.mark.solver]

SNAP = {"cran_snapshot": "2026-07-01"}


def _mk(tmp_path, pixi_bin, sshd_site, sub, prefer=None, ro_roots=None):
    w = Weft(tmp_path / f"ws-{sub}", pixi_bin=pixi_bin)
    cfg = {"host": sshd_site["host"], "port": sshd_site["port"],
           "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
           "root": f"/home/physicist/.weft-{sub}", "pixi_source": pixi_bin}
    if prefer:
        cfg["prefer"] = prefer
    if ro_roots:
        cfg["ro_roots"] = ro_roots
    w.register_site("beam", "ssh", cfg)
    w.runner.poll_interval = 0.3
    return w


def _tiny(linux_platforms):
    return {"name": "tiny-sq", "platforms": linux_platforms,
            "deps": {"conda": ["xz >=5"]}}


def test_realize_run_conformance_evict_concurrent(tmp_path, pixi_bin,
                                                  sshd_site, linux_platforms):
    w = _mk(tmp_path, pixi_bin, sshd_site, "sq", prefer="squashfs")
    caps = w.sites_describe("beam")["capabilities"]
    assert caps["squashfs"]["dev_fuse"] is True
    assert caps["squashfs"]["squashfuse"] and caps["squashfs"]["mksquashfs"]
    assert caps["squashfs"]["userns"] is True

    env = w.env_ensure(_tiny(linux_platforms))["env_id"]
    task = {"command": "xz --version > results/v.txt", "env": env,
            "outputs": ["results/"], "site": "beam"}
    r = w.task_submit(task)
    job = w.runner.wait(r["job_id"], 900)
    assert job["state"] == "DONE", job["error"]
    real = w.store.get_realization(env, "beam")
    assert real["strategy"] == "squashfs"
    a = w.adapters["beam"]
    assert a.file_exists(f"{real['location']}/image.sqfs")
    assert a.file_exists(f"jobs/{r['job_id']}/ns")   # ns-wrapped job
    out1 = next(o for o in job["manifest"]["outputs"]
                if o["path"] == "results/v.txt")["preview"]["lines"]
    # marker carries the image fence
    import json
    marker = json.loads(a.read_file(
        f"{real['location']}/.weft-ready").decode())
    assert marker["strategy"] == "squashfs"
    assert marker["image_sha256"] and marker["image_bytes"] > 0
    assert marker["bin_digest"] == f"sqfs:{marker['image_bytes']}"

    # conformance: the SAME EnvID under plain prefix → identical output
    w2 = _mk(tmp_path, pixi_bin, sshd_site, "px")
    env2 = w2.env_ensure(_tiny(linux_platforms))["env_id"]
    assert env2 == env
    r2 = w2.task_submit(dict(task))
    job2 = w2.runner.wait(r2["job_id"], 900)
    assert job2["state"] == "DONE", job2["error"]
    assert w2.store.get_realization(env, "beam")["strategy"] == "prefix"
    out2 = next(o for o in job2["manifest"]["outputs"]
                if o["path"] == "results/v.txt")["preview"]["lines"]
    assert out1 == out2

    # evict = unmount + delete image; rebuild happens on demand
    out = w.env_evict(env, "beam")
    assert out["state"] == "evicted", out
    assert not a.file_exists(f"{real['location']}/image.sqfs")

    # rebuild under CONCURRENT use: 4 elements, one image, own namespaces
    ra = w.task_submit({**task, "command": "xz --version > results/v.txt",
                        "array": 4}, force=True)
    import time
    for _ in range(300):
        c = w.store.group_counts(ra["group"])
        if c["total"] >= 4 and c["done"] + c["failed"] >= 4:
            break
        time.sleep(0.5)
    assert w.store.group_counts(ra["group"])["done"] == 4, \
        w.store.group_counts(ra["group"])
    # no tmp litter next to the image
    lit = a.run_cmd(f"ls {real['location']} | grep -c 'tmp' || true")
    assert lit.out.strip() in ("", "0")


def test_kernel_runs_inside_the_mount(tmp_path, pixi_bin, sshd_site,
                                      linux_platforms):
    w = _mk(tmp_path, pixi_bin, sshd_site, "kq", prefer="squashfs")
    env = w.env_ensure({"name": "pyk-sq", "platforms": linux_platforms,
                        "deps": {"conda": ["python =3.12"]}})["env_id"]
    r0 = w.task_submit({"command": "true", "env": env, "site": "beam"})
    assert w.runner.wait(r0["job_id"], 900)["state"] == "DONE"
    assert w.store.get_realization(env, "beam")["strategy"] == "squashfs"

    k = w.kernel_start("beam", "python", env_id=env,
                       label="squashfs kernel")["kernel_id"]
    try:
        r = w.kernel_exec(k, "import sys; print(sys.prefix)", timeout=120)
        assert r["rc"] == 0, r
        assert "/mnt/" in r["out"]      # the interpreter LIVES in the mount
    finally:
        w.kernel_stop(k)


def test_pypi_overlay_on_squashfs_parent(tmp_path, pixi_bin, sshd_site,
                                         linux_platforms):
    w = _mk(tmp_path, pixi_bin, sshd_site, "op", prefer="squashfs")
    parent = w.env_ensure({"name": "pybase-sq", "platforms": linux_platforms,
                           "deps": {"conda": ["python =3.12", "pip"]}})["env_id"]
    r0 = w.task_submit({"command": "true", "env": parent, "site": "beam"})
    assert w.runner.wait(r0["job_id"], 900)["state"] == "DONE"
    p_real = w.store.get_realization(parent, "beam")
    assert p_real["strategy"] == "squashfs"

    child = w.env_ensure({"name": "pybase-sq+emcee", "extends_env": parent,
                          "platforms": linux_platforms,
                          "deps": {"pypi": ["emcee"]}})
    assert child["delta"]["layerable"] is True, child
    r = w.task_submit({
        "command": "python -c 'import emcee, numpy; "
                   "print(emcee.__version__)' > results/v.txt",
        "env": child["env_id"], "outputs": ["results/"], "site": "beam"})
    job = w.runner.wait(r["job_id"], 1800)
    assert job["state"] == "DONE", job["error"]
    assert w.store.get_realization(child["env_id"], "beam")["strategy"] \
        == "overlay"
    out_sq = next(o for o in job["manifest"]["outputs"]
                  if o["path"] == "results/v.txt")["preview"]["lines"]
    assert out_sq and out_sq[0][0].isdigit()

    # the parent is READ-ONLY at the filesystem level: a write into the
    # mount must be refused by squashfs itself (EROFS), not by convention
    root = "/home/physicist/.weft-op"
    chk = w.site_exec(
        "beam",
        f". {root}/{p_real['location']}/activate.sh >/dev/null 2>&1; "
        f"touch {root}/{p_real['location']}/mnt/.write-probe 2>&1; "
        f"echo write_rc=$?",
        why="prove the squashfs parent is filesystem-enforced read-only")
    assert "write_rc=0" not in chk["stdout"], chk

    # conformance: same overlay on a PREFIX parent → identical output
    w2 = _mk(tmp_path, pixi_bin, sshd_site, "opx")
    parent2 = w2.env_ensure({"name": "pybase-sq",
                             "platforms": linux_platforms,
                             "deps": {"conda": ["python =3.12", "pip"]}})["env_id"]
    assert parent2 == parent
    r0 = w2.task_submit({"command": "true", "env": parent2, "site": "beam"})
    assert w2.runner.wait(r0["job_id"], 900)["state"] == "DONE"
    child2 = w2.env_ensure({"name": "pybase-sq+emcee", "extends_env": parent2,
                            "platforms": linux_platforms,
                            "deps": {"pypi": ["emcee"]}})
    assert child2["env_id"] == child["env_id"]
    r2 = w2.task_submit({
        "command": "python -c 'import emcee, numpy; "
                   "print(emcee.__version__)' > results/v.txt",
        "env": child2["env_id"], "outputs": ["results/"], "site": "beam"})
    job2 = w2.runner.wait(r2["job_id"], 1800)
    assert job2["state"] == "DONE", job2["error"]
    out_px = next(o for o in job2["manifest"]["outputs"]
                  if o["path"] == "results/v.txt")["preview"]["lines"]
    assert out_sq == out_px


@pytest.mark.slow
def test_cran_overlay_on_squashfs_parent(tmp_path, pixi_bin, sshd_site,
                                         linux_platforms):
    """R overlay over a read-only squashfs parent, including a COMPILED
    package: the weft toolchain builds against headers inside the mount
    and the artifact's rpath points into the mount path."""
    w = _mk(tmp_path, pixi_bin, sshd_site, "rq", prefer="squashfs")
    parent = w.env_ensure({"name": "rbase-sq", "platforms": linux_platforms,
                           "deps": {"conda": ["r-base =4.4"]},
                           "system_requirements": SNAP})["env_id"]
    r0 = w.task_submit({"command": "true", "env": parent, "site": "beam"})
    assert w.runner.wait(r0["job_id"], 1800)["state"] == "DONE"
    p_real = w.store.get_realization(parent, "beam")
    assert p_real["strategy"] == "squashfs"

    child = w.env_ensure({"name": "rbase-sq+jsonlite", "extends_env": parent,
                          "platforms": linux_platforms,
                          "deps": {"cran": ["jsonlite"]},
                          "system_requirements": SNAP})
    assert "env_id" in child, child
    assert child["delta"]["layerable"] is True, child
    r = w.task_submit({
        "command": "Rscript -e 'cat(jsonlite::toJSON(1:2))' > results/j.txt",
        "env": child["env_id"], "outputs": ["results/"], "site": "beam"})
    job = w.runner.wait(r["job_id"], 3600)
    assert job["state"] == "DONE", job["error"]
    assert w.store.get_realization(child["env_id"], "beam")["strategy"] \
        == "overlay"
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/j.txt")
    assert out["preview"]["lines"] == ["[1,2]"]

    root = "/home/physicist/.weft-rq"
    chk = w.site_exec(
        "beam",
        f". {root}/{p_real['location']}/activate.sh >/dev/null 2>&1; "
        f"touch {root}/{p_real['location']}/mnt/.write-probe 2>&1; "
        f"echo write_rc=$?",
        why="prove the squashfs R parent is filesystem-enforced read-only")
    assert "write_rc=0" not in chk["stdout"], chk


def test_ro_roots_adopts_squashfs_layout(tmp_path, pixi_bin, sshd_site,
                                         linux_platforms):
    """The institutional shape: admin realizes a squashfs env in a tree;
    another workspace adopts it read-only in place — image, marker and
    lazy-mount activation all travel with the layout."""
    wa = _mk(tmp_path, pixi_bin, sshd_site, "ra", prefer="squashfs")
    env = wa.env_ensure(_tiny(linux_platforms))["env_id"]
    r0 = wa.task_submit({"command": "true", "env": env, "site": "beam"})
    assert wa.runner.wait(r0["job_id"], 900)["state"] == "DONE"
    assert wa.store.get_realization(env, "beam")["strategy"] == "squashfs"

    wb = _mk(tmp_path, pixi_bin, sshd_site, "rb",
             ro_roots=["/home/physicist/.weft-ra"])
    env_b = wb.env_ensure(_tiny(linux_platforms))["env_id"]
    assert env_b == env
    r = wb.task_submit({"command": "xz --version > results/v.txt",
                        "env": env_b, "outputs": ["results/"],
                        "site": "beam"})
    job = wb.runner.wait(r["job_id"], 900)
    assert job["state"] == "DONE", job["error"]
    real = wb.store.get_realization(env_b, "beam")
    assert real["read_only"], real
    assert real["location"].startswith("/home/physicist/.weft-ra/"), real
