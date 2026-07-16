"""Pin-at-settlement (retention.md addendum): a live-run retain is a
recorded DECISION captured when the run settles — the user means the
eventual complete file, never a torn snapshot. Plus the label-aware
retained layout (runs/<label>/<target>/)."""

import json
import subprocess
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


def test_live_pin_captures_at_stop(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    r = w.kernel_exec(k, "open('umap.png', 'w').write('v1-plot')",
                      timeout=60)
    assert r["rc"] == 0
    pin = w.run_retain(k, include=["umap.png"], label="fig-run")
    assert pin["state"] == "pinned-pending"
    assert pin["matched_now"] == 1
    assert w.retained_runs(label="fig-run")[0]["state"] == "pinned-pending"

    # the file evolves before settlement — pin means the FINAL version
    w.kernel_exec(k, "open('umap.png', 'w').write('v2-final')", timeout=60)
    w.kernel_stop(k)

    row = w.retained_runs(label="fig-run")[0]
    assert row["state"] == "done"
    dest = Path(row["location"])
    assert (dest / "umap.png").read_text() == "v2-final"
    sidecar = json.loads((dest / ".weft-run.json").read_text())
    assert sidecar["target"] == k


def test_pin_before_file_exists(w):
    """Case 2 of the taxonomy: the user pins a filename they expect —
    the eventual complete file is captured."""
    k = w.kernel_start("local", "python")["kernel_id"]
    pin = w.run_retain(k, include=["final.rds"])
    assert pin["state"] == "pinned-pending" and pin["matched_now"] == 0
    w.kernel_exec(k, "open('final.rds', 'w').write('model-object')",
                  timeout=60)
    w.kernel_stop(k)
    row = w.retained_runs()[0]
    assert row["state"] == "done"
    assert (Path(row["location"]) / "final.rds").read_text() \
        == "model-object"


def test_directory_literal_pins_and_captures_as_a_unit(w):
    """A bare directory name (a .zarr-style store) is a first-class
    retention unit: the pin selects the whole subtree, the capture
    reconstructs it, and NO pin_missing fires for the literal."""
    k = w.kernel_start("local", "python")["kernel_id"]
    pin = w.run_retain(k, include=["out/embedding.zarr"], label="zarr")
    assert pin["state"] == "pinned-pending" and pin["matched_now"] == 0
    r = w.kernel_exec(
        k, "import os\n"
           "os.makedirs('out/embedding.zarr/g1', exist_ok=True)\n"
           "open('out/embedding.zarr/.zattrs', 'w').write('{}')\n"
           "open('out/embedding.zarr/g1/.zarray', 'w').write('{}')\n"
           "open('out/embedding.zarr/g1/0.0', 'w').write('chunk')\n"
           "open('unrelated.txt', 'w').write('no')", timeout=60)
    assert r["rc"] == 0
    w.kernel_stop(k)
    row = w.retained_runs(label="zarr")[0]
    assert row["state"] == "done"
    dest = Path(row["location"])
    assert (dest / "out/embedding.zarr/g1/0.0").read_text() == "chunk"
    assert (dest / "out/embedding.zarr/.zattrs").exists()
    assert not (dest / "unrelated.txt").exists()
    # sidecar enumerates the directory's files — the viewer's manifest
    sidecar = json.loads((dest / ".weft-run.json").read_text())
    zarr_files = {f["path"] for f in sidecar["files"]}
    assert zarr_files == {"out/embedding.zarr/.zattrs",
                          "out/embedding.zarr/g1/.zarray",
                          "out/embedding.zarr/g1/0.0"}
    # the literal was satisfied by its children: no false pin_missing
    ev = [e for e in w.events_poll(0, 500)["events"]
          if e["kind"] == "retain.pin_missing"]
    assert not ev, ev


def test_pin_never_materializes_is_honest(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    w.run_retain(k, include=["never.csv"])
    w.kernel_stop(k)
    row = w.retained_runs()[0]
    assert row["state"] == "failed"
    err = json.loads(row["error"])
    assert "never.csv" in str(err.get("missing"))
    ev = [e for e in w.events_poll(0, 500)["events"]
          if e["kind"] == "retain.pin_missing"]
    assert ev and "never.csv" in str(ev[0]["paths"])


def test_kernel_death_settles_pins(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    r = w.kernel_exec(k, "open('crash-result.txt', 'w').write('saved')",
                      timeout=60)
    assert r["rc"] == 0
    w.run_retain(k, include=["crash-result.txt"])
    jobdir = w.store.get_kernel(k)["jobdir"]
    pid = w.adapters["local"].read_file(f"{jobdir}/pid.real"
                                        ).decode().strip()
    subprocess.run(["kill", "-9", pid], check=True)
    deadline = time.time() + 45
    while time.time() < deadline:
        rows = w.retained_runs()
        if rows and rows[0]["state"] == "done":
            break
        time.sleep(0.3)
    assert rows[0]["state"] == "done"
    assert (Path(rows[0]["location"]) / "crash-result.txt").exists()


def test_running_job_pin_captures_at_completion(w):
    r = w.task_submit({"command": "echo early > partial.out; sleep 3; "
                                  "echo complete > partial.out",
                       "site": "local"})
    jid = r["job_id"]
    time.sleep(1)
    pin = w.run_retain(jid, include=["partial.out"])
    assert pin["state"] == "pinned-pending"
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    row = w.retained_runs()[0]
    assert row["state"] == "done"
    assert (Path(row["location"]) / "partial.out").read_text() \
        == "complete\n"                          # the eventual version


def test_discard_captures_pins_first(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    w.kernel_exec(k, "open('keep.txt', 'w').write('precious')", timeout=60)
    w.run_retain(k, include=["keep.txt"])
    # kill the settlement path's chance: stop settles, so pin is done —
    # instead forge a pending state to simulate a missed settlement
    w.kernel_stop(k)
    w.store.update_retained(k, state="pinned-pending")
    w.run_discard(k)
    row = w.retained_runs()[0]
    assert row["state"] == "done"                # captured before delete
    assert (Path(row["location"]) / "keep.txt").read_text() == "precious"
    jobdir = w.adapters["local"].path(w.store.get_kernel(k)["jobdir"])
    assert not Path(jobdir).exists()


def test_forget_cancels_pending_pin(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    w.run_retain(k, include=["someday.txt"], label="oops")
    out = w.run_forget(label="oops")
    assert out["forgotten"][0]["note"] == "pin cancelled before capture"
    assert w.retained_runs() == []
    w.kernel_stop(k)                             # settles nothing, calmly


def test_sweep_skips_stuck_pins(w):
    r = w.task_submit({"command": "echo x > f.txt", "site": "local"})
    jid = r["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    # forge a stuck pin on an aged terminal target
    w.store.put_retained(jid, "local", None, "/nowhere", False, 0, 0,
                         state="pinned-pending")
    w.store._write("UPDATE jobs SET updated_at=? WHERE job_id=?",
                   (time.time() - 30 * 86400, jid))
    w.gc_sweep("local", confirm=True)
    assert Path(w.adapters["local"].path(f"jobs/{jid}")).exists()  # spared
    ev = [e for e in w.events_poll(0, 500)["events"]
          if e["kind"] == "run.remains_skipped" and e["target"] == jid]
    assert ev and "pinned-pending" in ev[0]["why"]


def test_reconcile_settles_missed_pins(w, tmp_path, pixi_bin):
    k = w.kernel_start("local", "python")["kernel_id"]
    w.kernel_exec(k, "open('late.txt', 'w').write('rescued')", timeout=60)
    w.run_retain(k, include=["late.txt"])
    w.kernel_stop(k)
    w.store.update_retained(k, state="pinned-pending")  # settlement "missed"
    del w
    w2 = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    acts = w2.reconcile()
    assert any(a.get("action") == "settle-pin" for a in acts)
    row = w2.retained_runs()[0]
    assert row["state"] == "done"
    assert (Path(row["location"]) / "late.txt").read_text() == "rescued"


def test_label_layout_mirrors_host_runs(w):
    r = w.task_submit({"command": "echo fig > plot.svg", "site": "local"})
    jid = r["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    kept = w.run_retain(jid, include=["plot.svg"], label="enzyme-kinetics",
                        layout="label", background=False)
    dest = Path(kept["location"]["path"])
    assert dest.parts[-2:] == ("enzyme-kinetics", jid)
    assert (dest / "plot.svg").exists()
    # refusals: label layout without a label; unknown layout
    bad = w.run_retain(jid, include=["plot.svg"], layout="label")
    assert bad["error"] == "task.invalid" and "label" in bad["detail"]
    bad = w.run_retain(jid, include=["plot.svg"], layout="tree")
    assert bad["error"] == "task.invalid"


def test_block_dir_live_retain_still_immediate(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    r = w.kernel_exec(
        k, "import os\n"
           "open(os.environ['WEFT_BLOCK_DIR'] + '/t.csv', 'w')"
           ".write('x')", timeout=60)
    out = w.run_retain(k, include=["blocks/*.artifacts/**"],
                       background=False)
    assert out["state"] == "done"                # protocol-immutable: now
    w.kernel_stop(k)
