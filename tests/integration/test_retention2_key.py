"""retention2 R4: (run, relpath) as the universal key — file verbs
resolve sandbox → keep with an honest `at`; tasks take {"run","rel"}
inputs; declared outputs re-enter by their manifest ref (no rehash);
LINK anchors keeps so fetch and staging re-obtain evicted bytes."""

import base64
import json
import time
from pathlib import Path

import pytest

from weft.api import Weft


@pytest.fixture
def wA(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_bin": pixi_bin,
                                       "pixi_source": pixi_bin,
                                       "durable": True})
    w.runner.poll_interval = 0.2
    return w


@pytest.fixture
def wB(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def _run(w, cmd, outputs=None):
    r = w.task_submit({"command": cmd, "site": "local",
                       **({"outputs": outputs} if outputs else {})})
    job = w.runner.wait(r["job_id"], 120)
    assert job["state"] == "DONE", job.get("error")
    return r["job_id"], job


def test_stat_read_resolve_across_a_move(wB, tmp_path):
    """Topology B: file born in the sandbox, keep ships home, sandbox
    dies — the SAME (run, relpath) query works at every stage."""
    jid, _ = _run(wB, "echo v > results/out.dat", ["results/"])
    st = wB.run_file_stat(jid, "results/out.dat")
    assert st["exists"] and st["at"] == "sandbox"
    wB.run_retain(jid, include=["results/out.dat"], dest="@workspace",
                  background=False)
    # both copies exist; the sandbox answers first (precedence)
    assert wB.run_file_stat(jid, "results/out.dat")["at"] == "sandbox"
    wB.run_discard(jid)
    st = wB.run_file_stat(jid, "results/out.dat")
    assert st["exists"] and st["at"] == "retained"
    got = wB.run_file_read(jid, "results/out.dat")
    assert base64.b64decode(got["bytes_b64"]) == b"v\n"
    assert got["at"] == "retained"
    # forget the keep too: honest miss naming both fates
    wB.run_forget(target=jid)
    assert wB.run_file_stat(jid, "results/out.dat")["exists"] is False
    miss = wB.run_file_read(jid, "results/out.dat")
    assert miss["error"] == "data.missing"


def test_run_rel_task_inputs_chain_with_provenance(wA, tmp_path):
    jid1, job1 = _run(wA, "printf alpha > results/dataset.file.b",
                      ["results/"])
    ref_b = next(o["ref"] for o in job1["manifest"]["outputs"]
                 if o["path"] == "results/dataset.file.b")
    r = wA.task_submit({
        "command": "tr a-z A-Z < dataset.file.b > results/dataset.file.c",
        "inputs": [{"run": jid1, "rel": "results/dataset.file.b",
                    "mount_as": "dataset.file.b"}],
        "outputs": ["results/"], "site": "local"})
    assert "job_id" in r, r
    job2 = wA.runner.wait(r["job_id"], 120)
    assert job2["state"] == "DONE", job2.get("error")
    out_c = next(o for o in job2["manifest"]["outputs"]
                 if o["path"] == "results/dataset.file.c")
    got = wA.run_file_read(r["job_id"], "results/dataset.file.c")
    assert base64.b64decode(got["bytes_b64"]) == b"ALPHA"
    # the task hash keyed on the REF — provenance walks run2 -> run1
    prov = wA.provenance(out_c["ref"], depth=6)
    assert jid1 in json.dumps(prov)
    # and the resolved input IS the manifest ref (no new identity)
    assert ref_b in json.dumps(prov)


def test_declared_output_reenters_by_manifest_ref(wA):
    jid, job = _run(wA, "printf stable > results/model.bin", ["results/"])
    ref = next(o["ref"] for o in job["manifest"]["outputs"]
               if o["path"] == "results/model.bin")
    got = wA.data_register(run=jid, rel="results/model.bin")
    assert got["ref"] == ref
    assert "no rehash" in got["note"]


def test_undeclared_file_registers_lazily_with_lineage(wA):
    jid, _ = _run(wA, "printf loose > extra.dat")     # NOT declared
    got = wA.data_register(run=jid, rel="extra.dat")
    assert got["ref"].startswith("dref:")
    row = wA.store.get_dataref(got["ref"])
    assert row["meta"]["origin"] == f"run:{jid}/extra.dat"


def test_key_register_refusals(wA):
    jid, _ = _run(wA, "true")
    out = wA.data_register(run=jid, rel="never-existed.bin")
    assert out["error"] == "data.missing"
    assert out["hints"]["existed"] is False
    out = wA.data_register(path="/x", run=jid, rel="y")
    assert out["error"] == "task.invalid"
    out = wA.data_register(run=jid)
    assert out["error"] == "task.invalid"


def test_link_anchors_marked_keep_for_fetch_after_eviction(wA, tmp_path):
    """The LINK dividend: evict the CAS blob; fetch re-obtains from the
    marked keep by known hash — no rehash, no re-run."""
    jid, job = _run(wA, "printf precious > results/keep.bin",
                    ["results/"])
    ref = next(o["ref"] for o in job["manifest"]["outputs"]
               if o["path"] == "results/keep.bin")
    wA.run_retain(jid, include=["results/keep.bin"], background=False)
    row = wA.store.get_dataref(ref)
    assert row["meta"]["keep"]["target"] == jid          # anchored
    # evict every cached copy: site CAS blob + location row
    digest = ref.split(":")[-1]
    blob = tmp_path / "site" / "cas" / digest[:2] / digest
    blob.unlink()
    wA.store.demote_location(ref, "local")
    dest = tmp_path / "back.bin"
    wA.data_fetch(ref, str(dest))
    assert dest.read_text() == "precious"
    ev = [e for e in wA.events_poll(0, 800)["events"]
          if e["kind"] == "data.reobtained_from_keep"]
    assert ev and ev[0]["ref"] == ref


def test_link_anchors_home_keep_and_staging_reobtains(wB, tmp_path):
    """Topology B: keep shipped home is the re-obtainability anchor —
    a later task on the site re-stages the evicted output from it."""
    jid, job = _run(wB, "printf homeward > results/out.bin", ["results/"])
    ref = next(o["ref"] for o in job["manifest"]["outputs"]
               if o["path"] == "results/out.bin")
    wB.run_retain(jid, include=["results/out.bin"], dest="@workspace",
                  background=False)
    keep = wB.store.get_dataref(ref)["meta"]["keep"]
    assert keep["site"] == "@workspace"
    assert Path(keep["path"]).read_text() == "homeward"
    # simulate the scratch purge: sandbox + CAS blob + location gone
    wB.run_discard(jid)
    digest = ref.split(":")[-1]
    (tmp_path / "site" / "cas" / digest[:2] / digest).unlink()
    wB.store.demote_location(ref, "local")
    # a new task on the site consumes the ref: staging re-obtains
    r = wB.task_submit({"command": "cat in.bin > results/echo.txt",
                        "inputs": [{"ref": ref, "mount_as": "in.bin"}],
                        "outputs": ["results/"], "site": "local"})
    job2 = wB.runner.wait(r["job_id"], 120)
    assert job2["state"] == "DONE", job2.get("error")
    got = wB.run_file_read(r["job_id"], "results/echo.txt")
    assert base64.b64decode(got["bytes_b64"]) == b"homeward"


def test_damaged_keep_is_refused_not_served(wA, tmp_path):
    jid, job = _run(wA, "printf original > results/f.bin", ["results/"])
    ref = next(o["ref"] for o in job["manifest"]["outputs"]
               if o["path"] == "results/f.bin")
    wA.run_retain(jid, include=["results/f.bin"], background=False)
    # damage the keep, evict the caches
    (tmp_path / "site" / "jobs" / jid / "results" / "f.bin"
     ).write_text("TAMPERED")
    digest = ref.split(":")[-1]
    (tmp_path / "site" / "cas" / digest[:2] / digest).unlink()
    wA.store.demote_location(ref, "local")
    out = wA.data_fetch(ref, str(tmp_path / "x.bin"))
    assert out["error"] == "data.verify_failed"
    assert out["hints"]["source"] == "keep"
    assert not (tmp_path / "x.bin").exists()


def test_kernel_keep_chains_by_key(wA):
    """aba's norm: kernels declare nothing; the key still chains —
    identity arrives lazily with run: lineage."""
    k = wA.kernel_start("local", "python")["kernel_id"]
    r = wA.kernel_exec(k, "open('table.csv','w').write('a,b\\n1,2\\n')",
                       timeout=60)
    assert r["rc"] == 0
    wA.kernel_stop(k)
    wA.run_retain(k, background=False)          # mark all MY files
    r2 = wA.task_submit({
        "command": "wc -l < table.csv > results/n.txt",
        "inputs": [{"run": k, "rel": "table.csv"}],   # mount_as defaults
        "outputs": ["results/"], "site": "local"})
    job = wA.runner.wait(r2["job_id"], 120)
    assert job["state"] == "DONE", job.get("error")
    got = wA.run_file_read(r2["job_id"], "results/n.txt")
    assert base64.b64decode(got["bytes_b64"]).strip() == b"2"
    # lineage walks through the kernel run
    ref = next(o["ref"] for o in job["manifest"]["outputs"]
               if o["path"] == "results/n.txt")
    prov = wA.provenance(ref, depth=6)
    assert k in json.dumps(prov)