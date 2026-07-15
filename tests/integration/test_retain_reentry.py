"""Retention R5 (misc/retention.md): retained files typically feed
FURTHER calculations — re-entry preserves lineage (provenance walks
through the file into the producing run) and never moves bytes that are
already where the compute is."""

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


def test_lineage_survives_reentry(w):
    # run A produces a table; the agent retains it
    ra = w.task_submit({"command": "printf '1\\n2\\n3\\n' > table.csv",
                        "site": "local"})
    a_id = ra["job_id"]
    assert w.runner.wait(a_id, 120)["state"] == "DONE"
    kept = w.run_retain(a_id, include=["table.csv"], background=False)

    # weeks later: the retained file feeds run B
    retained_path = str(Path(kept["location"]["path"]) / "table.csv")
    reg = w.data_register(retained_path)
    ref = reg["ref"]
    rb = w.task_submit({"command": "wc -l < in.csv > results/n.txt",
                        "inputs": [{"ref": ref, "mount_as": "in.csv"}],
                        "outputs": ["results/"], "site": "local"})
    assert w.runner.wait(rb["job_id"], 120)["state"] == "DONE"

    # the chain walks THROUGH the retained file into run A
    p = w.provenance(rb["job_id"])
    inp = p["inputs"][0]
    assert inp["origin"] == f"run:{a_id}/table.csv"
    assert inp["produced_by"]["job_id"] == a_id
    assert "table.csv" in inp["produced_by"]["command"]


def test_kernel_lineage_notes_the_transcript(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    r = w.kernel_exec(
        k, "import os\n"
           "open(os.environ['WEFT_BLOCK_DIR'] + '/fit.txt', 'w')"
           ".write('mu=3.1')", timeout=60)
    w.kernel_stop(k)
    kept = w.run_retain(k, include=["blocks/*.artifacts/**"],
                        background=False)
    f = str(Path(kept["location"]["path"])
            / f"blocks/{r['block']:04d}.artifacts/fit.txt")
    ref = w.data_register(f)["ref"]
    node = w.provenance(ref)
    assert node["origin"].startswith(f"run:{k}/")
    assert node["produced_by"]["kernel_id"] == k
    assert "transcript" in node["produced_by"]["note"]


def test_site_side_registration_stages_zero_bytes(tmp_path, pixi_bin):
    """In-place retained file on a site with retain.dir: register it
    site-side, feed a new task on that site — the plan moves nothing."""
    keep = tmp_path / "longterm"
    keep.mkdir()
    w = Weft(tmp_path / "ws2", pixi_bin=pixi_bin)
    w.register_site("local", "local", {
        "root": str(tmp_path / "site2"), "pixi_source": pixi_bin,
        "retain": {"dir": str(keep)}})
    w.runner.poll_interval = 0.2
    ra = w.task_submit({"command": "echo 42,7 > model.csv",
                        "site": "local"})
    a_id = ra["job_id"]
    assert w.runner.wait(a_id, 120)["state"] == "DONE"
    kept = w.run_retain(a_id, include=["model.csv"], background=False)
    assert kept["in_place"]

    on_site = f"{kept['location']['path']}/model.csv"
    reg = w.data_register(on_site, site="local")
    assert reg["fetched_to"] == "local"
    # original stays browsable in place
    assert Path(on_site).read_text() == "42,7\n"
    # lineage carried on the site-side path too
    assert w.provenance(reg["ref"])["origin"] == f"run:{a_id}/model.csv"

    rb = w.task_submit({"command": "cat in/m.csv > results/copy.csv",
                        "inputs": [{"ref": reg["ref"],
                                    "mount_as": "in/m.csv"}],
                        "outputs": ["results/"], "site": "local"})
    assert rb["plan"]["staging"]["bytes_to_move"] == 0     # already there
    job = w.runner.wait(rb["job_id"], 120)
    assert job["state"] == "DONE"
    out = next(o for o in job["manifest"]["outputs"]
               if o["path"] == "results/copy.csv")
    assert out["preview"]["header"] == "42,7"    # csv → table preview
