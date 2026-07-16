"""Reference-in-place (aba ask B) + data_fingerprint (ask A) +
scaffold flags (#56): TB-scale data on stable storage gets identity
WITHOUT a cross-filesystem copy — symlink staging behind a stat-fence,
ingest only when bytes must move."""

import time
from pathlib import Path

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


@pytest.fixture
def home(tmp_path):
    """The 'stable storage' the site CAS does not live on."""
    d = tmp_path / "groups" / "lab" / "dataset"
    (d / "g1").mkdir(parents=True)
    (d / ".zattrs").write_text("{}")
    (d / "g1" / "0.0").write_text("chunk-zero")
    return d


def _run(w, task):
    r = w.task_submit(task)
    assert "job_id" in r, r
    job = w.runner.wait(r["job_id"], 120)
    assert job["state"] == "DONE", job.get("error")
    return r


# -- registration without ingest ------------------------------------------

def test_external_file_registers_with_no_copy(w, home, tmp_path):
    f = home / "g1" / "0.0"
    r = w.data_register(str(f), site="local", ingest=False)
    assert r["kind"] == "file" and r["external_home"] == str(f)
    assert "read-only BY CONTRACT" in r["note"]
    # NO blob landed in the site CAS
    digest = r["ref"].split(":")[-1]
    assert not (tmp_path / "site" / "cas" / digest[:2] / digest).exists()
    # the location says external, honestly
    loc = w.store.locations_of(r["ref"])[0]
    assert loc["path"] == f"external:{f}"


def test_external_tree_registers_with_no_copy(w, home, tmp_path):
    r = w.data_register(str(home), site="local", ingest=False)
    assert r["kind"] == "tree" and r["files"] == 2
    assert not any((tmp_path / "site" / "cas").rglob("*")), \
        "tree blobs must NOT be ingested"
    # identity is content: same convention as ingested registration
    r2 = w.data_register(str(home), site="local")     # ingest now
    assert r2["ref"] == r["ref"]


def test_ingest_false_needs_a_site(w):
    out = w.data_register("/some/path", ingest=False)
    assert out["error"] == "task.invalid"
    out = w.data_register("https://x.org/f.h5", site="local", ingest=False)
    assert out["error"] == "task.invalid"


# -- symlink staging + the fence ------------------------------------------

def test_task_stages_external_tree_as_symlink_zero_bytes(w, home, tmp_path):
    ref = w.data_register(str(home), site="local", ingest=False)["ref"]
    # the output content must differ from the input's, or collection
    # ingests an identical-digest blob and fakes an "input ingest"
    r = _run(w, {"command": "tr a-z A-Z < data/in/g1/0.0 > results/out.txt"
                            " && readlink data/in > results/link.txt",
                 "inputs": [{"ref": ref, "mount_as": "data/in"}],
                 "outputs": ["results/"], "site": "local"})
    assert r["plan"]["staging"]["bytes_to_move"] == 0
    jid = r["job_id"]
    sandbox = tmp_path / "site" / "jobs" / jid
    got = w.run_file_read(jid, "results/out.txt")
    import base64
    assert base64.b64decode(got["bytes_b64"]) == b"CHUNK-ZERO"
    link = base64.b64decode(
        w.run_file_read(jid, "results/link.txt")["bytes_b64"]).decode()
    assert link.strip() == str(home)          # a symlink, not a copy
    # the INPUT's blobs were never ingested (outputs are — chaining)
    for e in w.cas.tree_manifest(ref):
        if e["kind"] == "file":
            assert not (tmp_path / "site" / "cas" / e["sha256"][:2]
                        / e["sha256"]).exists(), \
                "same-site use must ingest no input bytes"


def test_fence_fails_staging_when_home_drifts(w, home):
    """Staging is async (the driver thread) — the fence failure lands
    on the JOB, before the command ever runs."""
    f = home / "g1" / "0.0"
    ref = w.data_register(str(f), site="local", ingest=False)["ref"]
    time.sleep(1.1)                            # mtime granularity
    f.write_text("chunk-zero-CHANGED")
    r = w.task_submit({"command": "true",
                       "inputs": [{"ref": ref, "mount_as": "in.dat"}],
                       "site": "local"})
    job = w.runner.wait(r["job_id"], 120)
    assert job["state"] == "FAILED"
    err = job["error"]
    assert err["error"] == "data.verify_failed"
    h = err["hints"]
    assert h["source"] == "external-home" and h["home"] == str(f)
    assert h["recorded"]["bytes"] == len("chunk-zero")
    assert h["observed"]["bytes"] == len("chunk-zero-CHANGED")
    assert "re-register" in h["suggestion"]


def test_fence_fails_when_home_vanishes(w, home):
    ref = w.data_register(str(home), site="local", ingest=False)["ref"]
    import shutil
    shutil.rmtree(home)
    r = w.task_submit({"command": "true",
                       "inputs": [{"ref": ref, "mount_as": "d"}],
                       "site": "local"})
    job = w.runner.wait(r["job_id"], 120)
    assert job["state"] == "FAILED"
    assert job["error"]["error"] == "data.verify_failed"
    assert job["error"]["hints"]["source"] == "external-home"


def test_memoization_holds_across_external_staging(w, home):
    ref = w.data_register(str(home), site="local", ingest=False)["ref"]
    task = {"command": "cat d/.zattrs > results/o.txt",
            "inputs": [{"ref": ref, "mount_as": "d"}],
            "outputs": ["results/"], "site": "local"}
    first = _run(w, task)
    again = w.task_submit(task)
    assert again.get("memoized") or again["job_id"] == first["job_id"]


# -- lazy ingest when bytes must move --------------------------------------

def test_fetch_home_ingests_on_demand(w, home, tmp_path):
    f = home / "g1" / "0.0"
    ref = w.data_register(str(f), site="local", ingest=False)["ref"]
    dest = tmp_path / "back.dat"
    w.data_fetch(ref, str(dest))
    assert dest.read_text() == "chunk-zero"
    # the move forced a site-side ingest: blob now CAS-backed, location
    # flipped, and the event trail says why
    digest = ref.split(":")[-1]
    assert (tmp_path / "site" / "cas" / digest[:2] / digest).exists()
    locs = {l["site"]: l["path"] for l in w.store.locations_of(ref)}
    assert not str(locs["local"]).startswith("external:")
    ev = [e for e in w.events_poll(0, 500)["events"]
          if e["kind"] == "data.ingested_for_transfer"]
    assert ev and ev[0]["ref"] == ref


def test_fetch_of_drifted_external_fails_the_fence(w, home, tmp_path):
    f = home / "g1" / "0.0"
    ref = w.data_register(str(f), site="local", ingest=False)["ref"]
    time.sleep(1.1)
    f.write_text("mutated")
    out = w.data_fetch(ref, str(tmp_path / "x.dat"))
    assert out["error"] == "data.verify_failed"
    assert out["hints"]["source"] == "external-home"
    assert not (tmp_path / "x.dat").exists()


# -- gc never touches external homes ---------------------------------------

def test_gc_skips_external_locations(w, home):
    ref = w.data_register(str(home), site="local", ingest=False)["ref"]
    # age the location record far past any cutoff
    w.store._write("UPDATE locations SET verified_at=? WHERE ref=?",
                   (time.time() - 365 * 86400, ref))
    plan = w.gc_plan("local")["sites"]["local"]
    assert ref not in {r["ref"] for r in plan["evictable_refs"]}
    w.gc_sweep("local", confirm=True)
    assert (home / ".zattrs").exists()         # the home is untouchable
    assert w.store.locations_of(ref)           # and the record survives


# -- data_fingerprint (ask A) ----------------------------------------------

def test_fingerprint_tree_and_file(w, home):
    fp = w.data_fingerprint(str(home), "local")
    assert fp["files"] == 2 and fp["truncated"] is False
    assert {e["path"] for e in fp["entries"]} == {".zattrs", "g1/0.0"}
    assert fp["total_bytes"] == 2 + len("chunk-zero")
    assert all("sha256" not in e for e in fp["entries"])   # stat-only
    # sampled hashing: only files under the threshold get hashed
    fp = w.data_fingerprint(str(home), "local", hash_under=5)
    by = {e["path"]: e for e in fp["entries"]}
    assert "sha256" in by[".zattrs"] and "sha256" not in by["g1/0.0"]
    # file root: one row at "." (shim v6)
    fp = w.data_fingerprint(str(home / ".zattrs"), "local")
    assert fp["files"] == 1 and fp["entries"][0]["path"] == "."
    # truncation honesty
    fp = w.data_fingerprint(str(home), "local", max_entries=1)
    assert fp["truncated"] is True and fp["listed"] == 1 and fp["files"] == 2


def test_fingerprint_missing_path_is_honest(w):
    out = w.data_fingerprint("/nowhere", "local")
    assert out["error"] == "data.missing"


# -- scaffold flags (#56) ----------------------------------------------------

def test_inventory_flags_scaffold_and_retain_all_means_my_files(w):
    r = w.task_submit({"command": "echo mine > result.csv",
                       "site": "local"})
    jid = r["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    inv = w.run_inventory(jid)
    by = {e["path"]: e for e in inv["entries"]}
    assert by["result.csv"].get("scaffold") is None       # the user's
    for p in ("cmd.sh", "activate.sh", "log"):
        assert by[p]["scaffold"] is True, p               # weft's
    # "retain all" keeps MY files only...
    kept = w.run_retain(jid, background=False)
    dest = Path(kept["location"]["path"])
    assert (dest / "result.csv").exists()
    assert not (dest / "cmd.sh").exists()
    assert not (dest / "log").exists()
    # ...but an explicit include is sovereign
    w.run_forget(target=jid)
    kept = w.run_retain(jid, include=["log", "result.csv"],
                        background=False)
    dest = Path(kept["location"]["path"])
    assert (dest / "log").exists() and (dest / "result.csv").exists()


@pytest.mark.docker
def test_remote_external_ref_end_to_end(tmp_path, pixi_bin, sshd_site):
    """The real topology: laptop controller, data on the cluster's
    stable path, weft root elsewhere. Register without ingest, run a
    task against a symlink, then fetch home — the move triggers the
    lazy source-side ingest and the wire hashes verify."""
    import subprocess
    w = Weft(tmp_path / "ws-r", pixi_bin=pixi_bin)
    w.register_site("beam", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.3
    a = w.adapters["beam"]
    stable = "/home/physicist/stable-data"
    a.run_cmd(f"mkdir -p {stable} && "
              f"printf 'big-bytes' > {stable}/measurements.dat")
    reg = w.data_register(f"{stable}/measurements.dat", site="beam",
                          ingest=False)
    assert reg["external_home"] == f"{stable}/measurements.dat"
    r = w.task_submit({"command": "readlink in.dat > results/l.txt && "
                                  "tr a-z A-Z < in.dat > results/up.txt",
                       "inputs": [{"ref": reg["ref"],
                                   "mount_as": "in.dat"}],
                       "outputs": ["results/"], "site": "beam"})
    job = w.runner.wait(r["job_id"], 300)
    assert job["state"] == "DONE", job.get("error")
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/l.txt")
    import base64
    # preview carries the head lines: prove the input was a symlink
    assert stable in str(out.get("preview"))
    # fetch home: lazy ingest at the source, verified transfer
    dest = tmp_path / "back.dat"
    w.data_fetch(reg["ref"], str(dest))
    assert dest.read_text() == "big-bytes"


def test_kernel_block_protocol_is_scaffold_artifacts_are_not(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    r = w.kernel_exec(
        k, "import os\n"
           "open(os.environ['WEFT_BLOCK_DIR'] + '/fig.png', 'w')"
           ".write('png')", timeout=60)
    assert r["rc"] == 0
    w.kernel_stop(k)
    inv = w.run_inventory(k)
    by = {e["path"]: e for e in inv["entries"]}
    code = f"blocks/{r['block']:04d}.code"
    art = f"blocks/{r['block']:04d}.artifacts/fig.png"
    assert by[code]["scaffold"] is True
    assert by[art].get("scaffold") is None
