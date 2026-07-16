"""Institutional publishing (phase 5 stage A): publish → catalog →
cross-USER adoption → extension of read-only squashfs bases.

The container gets a real second user (`analyst`, created via docker
exec) and a root-owned tree — the honest simulation of "admin publishes,
lab members consume and extend".
"""

import json
import subprocess

import pytest

from weft.api import Weft

pytestmark = [pytest.mark.docker, pytest.mark.solver]

TREE = "/srv/lab-envs"


def _exec(container: str, cmd: str) -> str:
    r = subprocess.run(["docker", "exec", container, "sh", "-c", cmd],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (cmd, r.stderr[-300:])
    return r.stdout


def _ensure_analyst(container: str) -> None:
    _exec(container,
          "id analyst >/dev/null 2>&1 || ("
          "useradd -m -s /bin/bash analyst && "
          "mkdir -p /home/analyst/.ssh && "
          "cp /home/physicist/.ssh/authorized_keys /home/analyst/.ssh/ && "
          "chown -R analyst:analyst /home/analyst/.ssh && "
          "chmod 700 /home/analyst/.ssh && "
          "chmod 600 /home/analyst/.ssh/authorized_keys)")


@pytest.fixture(scope="module")
def lab(tmp_path_factory, pixi_bin, sshd_site, linux_platforms):
    """Publisher (physicist) + a published python base in a tree that is
    then chowned root:root — plus the analyst user for cross-user tests."""
    tmp = tmp_path_factory.mktemp("publish")
    _exec(sshd_site["container"],
          f"mkdir -p {TREE} && chown physicist {TREE}")
    _ensure_analyst(sshd_site["container"])

    pub = Weft(tmp / "publisher", pixi_bin=pixi_bin)
    pub.register_site("beam", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": "/home/physicist/.weft-pub", "pixi_source": pixi_bin})
    pub.runner.poll_interval = 0.3

    py_base = pub.env_ensure({"name": "lab-py", "platforms": linux_platforms,
                              "deps": {"conda": ["python =3.12", "pip"]}})
    assert "env_id" in py_base, py_base
    r = pub.env_publish(py_base["env_id"], "beam", TREE,
                        name="lab-py", version="2026.07")
    assert r["image_bytes"] > 0, r
    # the fixture container has userns (SYS_ADMIN): lab-py builds via
    # BIND STAGING — every downstream test (adopt, run, extend, kernel)
    # is then proof that a staged-built image byte-works at the tree path
    assert r["staging"]["used"] is True, r
    a = pub.adapters["beam"]
    assert r["staging"]["dir"].startswith("/home/physicist/.weft-pub/")
    # scaffolding is gone; the tree kept only image + sidecars
    assert not a.file_exists(r["staging"]["dir"])
    h = py_base["env_id"].rsplit(":", 1)[-1]
    mnt_ls = a.run_cmd(f"ls -A {TREE}/envs/{h}/mnt").out.strip()
    assert mnt_ls == "", f"tree mountpoint not empty: {mnt_ls!r}"

    tiny = pub.env_ensure({"name": "lab-tiny", "platforms": linux_platforms,
                           "deps": {"conda": ["xz >=5"]}})
    assert "env_id" in tiny, tiny
    # the classic build-at-destination path stays covered
    r2 = pub.env_publish(tiny["env_id"], "beam", TREE,
                         name="lab-tiny", version="1", staging="none")
    assert r2["staging"] == {"used": False, "why": "staging disabled ('none')"}

    # the tree becomes ADMIN-OWNED, world-readable: the real deployment
    _exec(sshd_site["container"],
          f"chown -R root:root {TREE} && chmod -R a+rX {TREE}")

    return {"pub": pub, "tmp": tmp, "py_env": py_base["env_id"],
            "tiny_env": tiny["env_id"], "sshd": sshd_site}


def _analyst_site(tmp, pixi_bin, sshd, sub="a1"):
    w = Weft(tmp / f"analyst-{sub}", pixi_bin=pixi_bin)
    w.register_site("beam", "ssh", {
        "host": sshd["host"], "port": sshd["port"],
        "user": "analyst", "ssh_opts": sshd["ssh_opts"],
        "root": "/home/analyst/.weft", "pixi_source": pixi_bin,
        "ro_roots": [TREE]})
    w.runner.poll_interval = 0.3
    return w


def test_publish_artifacts_and_refusals(lab):
    pub = lab["pub"]
    a = pub.adapters["beam"]
    cat = json.loads(a.read_file(f"{TREE}/catalog.json").decode())
    assert cat["envs"]["lab-py"]["latest"] == "2026.07"
    rec = cat["envs"]["lab-py"]["versions"]["2026.07"]
    assert rec["env_id"] == lab["py_env"] and rec["image_sha256"]
    # write-time render facts recorded IN the catalog (weft-ui ask):
    # grade + spec summary are artifact facts, not read-time computation
    assert rec["grade"] in ("fully-pinned", "snapshot-pinned")
    assert rec["spec_summary"]["spec_name"] == "lab-py"
    assert rec["spec_summary"]["packages_per_platform"]
    h = lab["py_env"].rsplit(":", 1)[-1]
    assert a.file_exists(f"{TREE}/envs/{h}/image.sqfs")
    assert a.file_exists(f"{TREE}/locks/{h}.json")
    # the publisher's own render view: latest yes, but publish leaves no
    # realization row (build-at-tree, not a workspace realization) —
    # state_here honestly says missing until the publisher USES it
    row = pub.env_published("beam", tree=TREE)
    vrec = row["envs"]["lab-py"]["versions"]["2026.07"]
    assert vrec["is_latest"] is True
    assert vrec["state_here"] == "missing"

    # refusals: tree inside the weft root; unknown env; bad name
    bad = pub.env_publish(lab["py_env"], "beam",
                          "/home/physicist/.weft-pub/sub", "x", "1")
    assert bad["error"] == "task.invalid" and "OUTSIDE" in bad["detail"]
    bad = pub.env_publish("env:v1:" + "0" * 64, "beam", TREE, "x", "1")
    assert bad["error"] == "task.invalid"
    bad = pub.env_publish(lab["py_env"], "beam", TREE, "bad name!", "1")
    assert bad["error"] == "task.invalid"


def test_analyst_adopts_by_name_and_runs(lab, pixi_bin):
    w = _analyst_site(lab["tmp"], pixi_bin, lab["sshd"])
    got = w.env_adopt("beam", TREE, "lab-py")
    assert got["env_id"] == lab["py_env"]
    assert "warning" not in got, got            # ro_roots is configured
    # the analyst's workspace never saw the spec — no solver ran
    assert w.store.get_env(lab["py_env"])["native_lock"]

    r = w.task_submit({"command": "python -c 'print(6*7)' > results/o.txt",
                       "env": got["env_id"], "outputs": ["results/"],
                       "site": "beam"})
    job = w.runner.wait(r["job_id"], 900)
    assert job["state"] == "DONE", job["error"]
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/o.txt")
    assert out["preview"]["lines"] == ["42"]
    real = w.store.get_realization(got["env_id"], "beam")
    assert real["read_only"], real
    assert real["location"].startswith(TREE + "/"), real
    assert real["strategy"] == "squashfs"
    # after first use, the analyst's render row says so: one call, no
    # host-side joins (weft-ui ask)
    vrec = w.env_published("beam", tree=TREE)["envs"]["lab-py"] \
        ["versions"]["2026.07"]
    assert vrec["state_here"] == "adopted-ro" and vrec["last_used"]
    assert vrec["is_latest"] is True


def test_analyst_extends_the_readonly_base(lab, pixi_bin, linux_platforms):
    """THE institutional promise: a lab member adds their package on top
    of the admin-owned, filesystem-read-only base — without owning any
    of it."""
    w = _analyst_site(lab["tmp"], pixi_bin, lab["sshd"], sub="ext")
    parent = w.env_adopt("beam", TREE, "lab-py")["env_id"]
    r0 = w.task_submit({"command": "true", "env": parent, "site": "beam"})
    assert w.runner.wait(r0["job_id"], 900)["state"] == "DONE"

    child = w.env_ensure({"name": "mine", "extends_env": parent,
                          "platforms": linux_platforms,
                          "deps": {"pypi": ["emcee"]}})
    assert "env_id" in child, child
    assert child["delta"]["layerable"] is True, child
    r = w.task_submit({
        "command": "python -c 'import emcee; print(emcee.__version__)' "
                   "> results/v.txt",
        "env": child["env_id"], "outputs": ["results/"], "site": "beam"})
    job = w.runner.wait(r["job_id"], 1800)
    assert job["state"] == "DONE", job["error"]
    real = w.store.get_realization(child["env_id"], "beam")
    assert real["strategy"] == "overlay", real
    # the overlay's own bytes live in the ANALYST's root; the parent's
    # in the admin tree, untouched
    assert real["location"].startswith("envs/"), real

    # and the base is filesystem-enforced read-only for the analyst
    h = parent.rsplit(":", 1)[-1]
    chk = w.site_exec(
        "beam",
        f". {TREE}/envs/{h}/activate.sh >/dev/null 2>&1; "
        f"touch {TREE}/envs/{h}/mnt/.probe 2>&1; echo write_rc=$?",
        why="prove the published base is read-only for consumers")
    assert "write_rc=0" not in chk["stdout"], chk


def test_kernel_on_adopted_env(lab, pixi_bin):
    w = _analyst_site(lab["tmp"], pixi_bin, lab["sshd"], sub="krn")
    env = w.env_adopt("beam", TREE, "lab-py")["env_id"]
    r0 = w.task_submit({"command": "true", "env": env, "site": "beam"})
    assert w.runner.wait(r0["job_id"], 900)["state"] == "DONE"
    k = w.kernel_start("beam", "python", env_id=env,
                       label="analyst on published base")["kernel_id"]
    try:
        r = w.kernel_exec(k, "import sys; print(sys.prefix)", timeout=120)
        assert r["rc"] == 0 and TREE in r["out"], r
    finally:
        w.kernel_stop(k)


def test_version_flip_and_pinned_old(lab, pixi_bin, linux_platforms):
    pub = lab["pub"]
    # v2 of lab-tiny is a different spec → different EnvID
    tiny2 = pub.env_ensure({"name": "lab-tiny", "platforms": linux_platforms,
                            "deps": {"conda": ["xz >=5", "zlib"]}})
    assert tiny2["env_id"] != lab["tiny_env"]
    # the tree is root-owned now: publishing v2 needs the admin hat —
    # simulate by reopening the tree for the publisher
    _exec(lab["sshd"]["container"],
          f"chown -R physicist {TREE}")
    r = pub.env_publish(tiny2["env_id"], "beam", TREE,
                        name="lab-tiny", version="2")
    assert r["version"] == "2"
    _exec(lab["sshd"]["container"],
          f"chown -R root:root {TREE} && chmod -R a+rX {TREE}")

    w = _analyst_site(lab["tmp"], pixi_bin, lab["sshd"], sub="ver")
    latest = w.env_adopt("beam", TREE, "lab-tiny")
    assert latest["version"] == "2" and latest["env_id"] == tiny2["env_id"]
    pinned = w.env_adopt("beam", TREE, "lab-tiny", version="1")
    assert pinned["env_id"] == lab["tiny_env"]


def test_unpublish_grace_then_purge(lab, pixi_bin):
    pub = lab["pub"]
    _exec(lab["sshd"]["container"], f"chown -R physicist {TREE}")
    out = pub.env_unpublish("beam", TREE, "lab-tiny", "1")
    assert out["state"] == "unpublished"
    a = pub.adapters["beam"]
    h = lab["tiny_env"].rsplit(":", 1)[-1]
    assert a.file_exists(f"{TREE}/envs/{h}/image.sqfs")   # grace period

    w = _analyst_site(lab["tmp"], pixi_bin, lab["sshd"], sub="gone")
    miss = w.env_adopt("beam", TREE, "lab-tiny", version="1")
    assert miss["error"] == "data.missing"
    assert w.env_adopt("beam", TREE, "lab-tiny")["version"] == "2"

    out = pub.env_unpublish("beam", TREE, "lab-tiny", "2", purge=True)
    assert out["state"] == "purged"
    h2 = out["env_id"].rsplit(":", 1)[-1]
    assert not a.file_exists(f"{TREE}/envs/{h2}/image.sqfs")
