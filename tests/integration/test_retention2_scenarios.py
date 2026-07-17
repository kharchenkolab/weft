"""retention2 walkthroughs as literal tests — full lifecycle sequences
(ingest → run → retain → chain → view → delete) across the three
topologies, local and remote, with honest accounting at every stage."""

import base64
import json
from pathlib import Path

import pytest

from weft.api import Weft


def _run(w, task):
    r = w.task_submit(task)
    assert "job_id" in r, r
    job = w.runner.wait(r["job_id"], 300)
    assert job["state"] == "DONE", job.get("error")
    return r["job_id"], job


def _out_ref(job, path):
    return next(o["ref"] for o in job["manifest"]["outputs"]
                if o["path"] == path)


def test_walkthrough_A_durable_root(tmp_path, pixi_bin):
    """Everything stays on the node; nothing ever moves; every deletion
    is explicit and its aftermath honest."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("hpc", "local", {"root": str(tmp_path / "site"),
                                     "pixi_source": pixi_bin,
                                     "durable": True})
    w.runner.poll_interval = 0.2

    # 1. "fetch a dataset" — stand-in for the URL: register a local file
    src = tmp_path / "dataset.file.a"
    src.write_text("raw,signal\n1,9\n2,7\n")
    ref_a = w.data_register(str(src))["ref"]

    # 2. run 1: a -> b. manifest names both the path and the ref
    jid1, job1 = _run(w, {
        "command": "tr , ';' < dataset.file.a > results/dataset.file.b",
        "inputs": [{"ref": ref_a, "mount_as": "dataset.file.a"}],
        "outputs": ["results/"], "site": "hpc", "label": "proj9 step1"})
    ref_b = _out_ref(job1, "results/dataset.file.b")
    inv = w.run_inventory(jid1)
    assert "results/dataset.file.b" in {e["path"] for e in inv["entries"]}

    # 3. retain b: MARK — zero movement, the manifest path stays THE path
    r = w.run_retain(jid1, include=["results/dataset.file.b"],
                     label="proj9", background=False)
    assert r["moved"] is False and r["state"] == "done"
    b_path = tmp_path / "site" / "jobs" / jid1 / "results/dataset.file.b"
    assert b_path.read_text() == "raw;signal\n1;9\n2;7\n"

    # 4. run 2: b -> c, addressed by the (run, relpath) key
    jid2, job2 = _run(w, {
        "command": "wc -l < dataset.file.b > results/dataset.file.c",
        "inputs": [{"run": jid1, "rel": "results/dataset.file.b",
                    "mount_as": "dataset.file.b"}],
        "outputs": ["results/"], "site": "hpc", "label": "proj9 step2"})
    ref_c = _out_ref(job2, "results/dataset.file.c")
    assert r["plan"]["staging"]["bytes_to_move"] == 0 \
        if "plan" in r else True

    # 5. retain c
    w.run_retain(jid2, include=["results/dataset.file.c"],
                 label="proj9", background=False)
    assert len(w.retained_runs(label="proj9")) == 2

    # 6. view c at home: preview by key, full copy by ref
    got = w.run_file_read(jid2, "results/dataset.file.c")
    assert base64.b64decode(got["bytes_b64"]).strip() == b"3"
    dest = tmp_path / "local-view.file.c"
    w.data_fetch(ref_c, str(dest))
    assert dest.read_text().strip() == "3"

    # 7. delete EVERYTHING from run 2, including its keep
    fo = w.run_forget(target=jid2)
    assert fo["bytes_reclaimed"] == 0             # unmark deletes nothing
    w.run_discard(jid2)
    assert not (tmp_path / "site" / "jobs" / jid2).exists()
    # the honest aftermath:
    assert w.run_inventory(jid2)["entries"]       # knowledge survives
    assert dest.exists()                          # the user's copy is theirs
    assert b_path.exists()                        # run 1's keep untouched
    assert w.run_file_stat(jid1, "results/dataset.file.b")["exists"]
    assert w.retained_runs(label="proj9")[0]["target"] == jid1


@pytest.mark.docker
def test_walkthrough_B_scratch_root_roundtrip(tmp_path, pixi_bin,
                                              sshd_site):
    """Nothing durable on the remote: the refusal asks once; keeps ship
    home; the home keep re-anchors the chain after a scratch purge."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("beam", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.3

    jid1, job1 = _run(w, {
        "command": "printf alpha-bytes > results/dataset.file.b",
        "outputs": ["results/"], "site": "beam", "label": "p step1"})
    ref_b = _out_ref(job1, "results/dataset.file.b")

    # 3. the refusal fires first — then the explicit decision
    out = w.run_retain(jid1, include=["results/dataset.file.b"],
                       background=False)
    assert out["error"] == "retain.no_durable"
    r = w.run_retain(jid1, include=["results/dataset.file.b"],
                     label="p", dest="@workspace", background=False)
    assert r["moved"] is True and r["state"] == "done"
    home_b = Path(r["location"]["path"]) / "results/dataset.file.b"
    assert home_b.read_text() == "alpha-bytes"

    # simulate the scratch purge: sandbox gone, CAS blob gone
    w.run_discard(jid1)
    a = w.adapters["beam"]
    digest = ref_b.split(":")[-1]
    a.run_cmd(f"rm -f {a.path(f'cas/{digest[:2]}/{digest}')}")
    w.store.demote_location(ref_b, "beam")

    # 4. run 2 chains on b by key: staging re-obtains from the HOME keep
    jid2, job2 = _run(w, {
        "command": "tr a-z A-Z < dataset.file.b > results/dataset.file.c",
        "inputs": [{"run": jid1, "rel": "results/dataset.file.b",
                    "mount_as": "dataset.file.b"}],
        "outputs": ["results/"], "site": "beam", "label": "p step2"})

    # 5-6. retain c home; the keep IS the host copy — no fetch step
    r2 = w.run_retain(jid2, include=["results/dataset.file.c"],
                      label="p", dest="@workspace", background=False)
    home_c = Path(r2["location"]["path"]) / "results/dataset.file.c"
    assert home_c.read_text() == "ALPHA-BYTES"

    # 7. delete run 2 incl. its keep: forget deletes the home copies
    w.run_forget(target=jid2)
    assert not home_c.exists()
    w.run_discard(jid2)
    assert w.run_inventory(jid2)["entries"]       # knowledge survives
    assert home_b.exists()                        # run 1's keep untouched


@pytest.mark.docker
def test_walkthrough_C_two_tiers_on_the_node(tmp_path, pixi_bin,
                                             sshd_site):
    """Fast scratch root + durable path on the same node: retain hops
    site-side (never crosses the wire); the keep serves later runs."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("beam", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin,
        "durable": "/home/physicist/durable-keeps"})
    w.runner.poll_interval = 0.3
    a = w.adapters["beam"]

    jid1, job1 = _run(w, {
        "command": "printf tier-data > results/model.bin",
        "outputs": ["results/"], "site": "beam", "label": "t"})
    ref = _out_ref(job1, "results/model.bin")

    r = w.run_retain(jid1, include=["results/model.bin"], label="t",
                     layout="label", background=False)
    assert r["moved"] is True and r["in_place"] is True
    keep = f"/home/physicist/durable-keeps/runs/t/{jid1}"
    assert r["location"]["path"] == keep
    chk = a.run_cmd(f"cat {keep}/results/model.bin")
    assert chk.out == "tier-data"
    # nothing landed on the controller
    assert not (tmp_path / "ws" / "runs").exists()

    # purge the scratch side entirely; chain by key from the keep
    w.run_discard(jid1)
    digest = ref.split(":")[-1]
    a.run_cmd(f"rm -f {a.path(f'cas/{digest[:2]}/{digest}')}")
    w.store.demote_location(ref, "beam")
    jid2, job2 = _run(w, {
        "command": "wc -c < model.bin > results/size.txt",
        "inputs": [{"run": jid1, "rel": "results/model.bin",
                    "mount_as": "model.bin"}],
        "outputs": ["results/"], "site": "beam"})
    got = w.run_file_read(jid2, "results/size.txt")
    assert base64.b64decode(got["bytes_b64"]).strip() == b"9"

    # the (run, relpath) key answers from the keep after the purge
    st = w.run_file_stat(jid1, "results/model.bin")
    assert st["exists"] and st["at"] == "retained"

    # forget deletes the keep tree site-side; sandbox already gone
    w.run_forget(target=jid1)
    chk = a.run_cmd(f"test -e {keep} && echo alive || echo gone")
    assert chk.out.strip() == "gone"


@pytest.mark.docker
def test_mixed_site_chain_through_home_keep(tmp_path, pixi_bin,
                                            sshd_site):
    """Remote result, kept home, consumed by a LOCAL run via the key —
    the keep is the bridge between sites."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("beam", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin})
    w.register_site("laptop", "local", {"root": str(tmp_path / "lsite"),
                                        "pixi_source": pixi_bin,
                                        "durable": True})
    w.runner.poll_interval = 0.3

    jid1, job1 = _run(w, {"command": "printf remote-made > results/r.dat",
                          "outputs": ["results/"], "site": "beam"})
    w.run_retain(jid1, include=["results/r.dat"], dest="@workspace",
                 background=False)
    # remote side dies completely
    w.run_discard(jid1)
    a = w.adapters["beam"]
    ref = _out_ref(job1, "results/r.dat")
    digest = ref.split(":")[-1]
    a.run_cmd(f"rm -f {a.path(f'cas/{digest[:2]}/{digest}')}")
    w.store.demote_location(ref, "beam")

    jid2, job2 = _run(w, {
        "command": "tr a-z A-Z < r.dat > results/up.txt",
        "inputs": [{"run": jid1, "rel": "results/r.dat",
                    "mount_as": "r.dat"}],
        "outputs": ["results/"], "site": "laptop"})
    got = w.run_file_read(jid2, "results/up.txt")
    assert base64.b64decode(got["bytes_b64"]) == b"REMOTE-MADE"