"""retention2 (misc/retention2.md): retain marks; storage moves only
when storage demands it. The durable site key, mark-in-place, the
retain.no_durable refusal, forget-as-inverse, selective discard."""

import json
import os
import time
from pathlib import Path

import pytest

from weft.api import Weft
from weft.retain import storage_facts


# -- R1: the durable key ---------------------------------------------------

def test_storage_facts_tristate_and_validation():
    assert storage_facts({"durable": True})["durable"] is True
    assert storage_facts({"durable": "/users/u/keeps"})["durable"] \
        == "/users/u/keeps"
    assert storage_facts({})["durable"] is None
    assert storage_facts({"durable": False})["durable"] is None
    # legacy alias
    f = storage_facts({"retain": {"dir": "/k"}})
    assert f["durable"] == "/k" and "deprecated" in f["source"]
    # relative path refused
    from weft.errors import WeftError
    with pytest.raises(WeftError):
        storage_facts({"durable": "relative/path"})
    with pytest.raises(WeftError):
        storage_facts({"durable": 42})
    # the heuristic only phrases hints, never decides
    assert "home" in storage_facts({"root": "/users/u/.weft"})["hint"]
    assert "scratch" in storage_facts({"root": "/scratch/u/.weft"})["hint"]


def test_registration_echoes_storage(tmp_path, pixi_bin, monkeypatch):
    # this test is about the STORAGE echo; the best-effort site-tools
    # acquisition (network fetch + --version probe) is orthogonal and
    # flaky under full-lane load — stub it
    import weft.site_tools
    monkeypatch.setattr(weft.site_tools, "ensure_site_tools",
                        lambda *a, **k: {})
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    r = w.register_site("local", "local", {
        "root": str(tmp_path / "site"), "pixi_source": pixi_bin,
        "durable": True})
    assert r["storage"]["durable"] is True
    assert w.sites_describe("local")["storage"]["durable"] is True

    r = w.register_site("l2", "local", {
        "root": str(tmp_path / "site2"), "pixi_source": pixi_bin,
        "durable": str(tmp_path / "keeps")})
    assert r["storage"]["durable"] == str(tmp_path / "keeps")
    assert "warning" not in r["storage"]
    assert (tmp_path / "keeps").is_dir()      # created + verified writable


# -- fixtures for the three topologies ------------------------------------

@pytest.fixture
def wA(tmp_path, pixi_bin):
    """Topology A: durable root."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin,
                                       "durable": True})
    w.runner.poll_interval = 0.2
    return w


@pytest.fixture
def wB(tmp_path, pixi_bin):
    """Topology B: nothing durable."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


@pytest.fixture
def wC(tmp_path, pixi_bin):
    """Topology C: scratch root + durable path."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin,
                                       "durable": str(tmp_path / "keeps")})
    w.runner.poll_interval = 0.2
    return w


def _run(w, cmd):
    r = w.task_submit({"command": cmd, "site": "local"})
    assert w.runner.wait(r["job_id"], 120)["state"] == "DONE"
    return r["job_id"]


# -- R2: mark in place (topology A) ----------------------------------------

def test_mark_moves_nothing_and_paths_stay(wA, tmp_path):
    jid = _run(wA, "echo keep > result.csv && echo junk > tmp.log")
    before = os.stat(tmp_path / "site" / "jobs" / jid / "result.csv")
    r = wA.run_retain(jid, include=["result.csv"], label="proj9",
                      background=False)
    assert r["moved"] is False and r["in_place"] is True
    assert r["state"] == "done" and r["files"] == 1
    assert r["location"]["path"] == str(tmp_path / "site" / "jobs" / jid)
    # the inode proof: same file, same place, nothing copied
    after = os.stat(tmp_path / "site" / "jobs" / jid / "result.csv")
    assert (before.st_ino, before.st_mtime) == (after.st_ino,
                                                after.st_mtime)
    assert not (tmp_path / "ws" / "runs").exists()   # nothing shipped
    # sidecar sits IN the jobdir; catalog row says mark
    sidecar = json.loads((tmp_path / "site" / "jobs" / jid /
                          ".weft-run.json").read_text())
    assert sidecar["method"] == "mark"
    assert {f["path"] for f in sidecar["files"]} == {"result.csv"}
    row = wA.retained_runs(label="proj9")[0]
    assert row["state"] == "done" and row["method"] == "mark"
    ev = [e for e in wA.events_poll(0, 500)["events"]
          if e["kind"] == "retain.marked"]
    assert ev and ev[0]["target"] == jid


def test_mark_forget_deletes_nothing(wA, tmp_path):
    jid = _run(wA, "echo keep > result.csv")
    wA.run_retain(jid, include=["result.csv"], background=False)
    out = wA.run_forget(target=jid)
    assert "unmarked" in out["forgotten"][0]["note"]
    assert out["bytes_reclaimed"] == 0
    # the FILES ARE STILL THERE — forget removed only the pin+sidecar
    jobdir = tmp_path / "site" / "jobs" / jid
    assert (jobdir / "result.csv").read_text() == "keep\n"
    assert not (jobdir / ".weft-run.json").exists()
    assert wA.retained_runs() == []


def test_mark_selective_discard_keeps_the_keeps(wA, tmp_path):
    jid = _run(wA, "mkdir -p figs && echo f > figs/a.svg && "
                   "echo junk > scratch.tmp && echo more > extra.dat")
    wA.run_retain(jid, include=["figs/**"], background=False)
    out = wA.run_discard(jid)
    assert out["selective"] is True
    jobdir = tmp_path / "site" / "jobs" / jid
    assert (jobdir / "figs" / "a.svg").read_text() == "f\n"   # kept
    assert (jobdir / ".weft-run.json").exists()                # receipt
    assert not (jobdir / "scratch.tmp").exists()               # junk gone
    assert not (jobdir / "extra.dat").exists()
    assert not (jobdir / "cmd.sh").exists()                    # scaffold too
    # full deletion = forget then discard
    wA.run_forget(target=jid)
    out = wA.run_discard(jid)
    assert "selective" not in out
    assert not jobdir.exists()
    # knowledge survives it all
    assert "figs/a.svg" in {e["path"] for e in
                            wA.run_inventory(jid)["entries"]}


def test_mark_retain_all_means_my_files(wA, tmp_path):
    jid = _run(wA, "echo mine > out.txt")
    r = wA.run_retain(jid, background=False)     # no include
    sidecar = json.loads((tmp_path / "site" / "jobs" / jid /
                          ".weft-run.json").read_text())
    paths = {f["path"] for f in sidecar["files"]}
    assert "out.txt" in paths
    assert not any(p in paths for p in ("cmd.sh", "log", "activate.sh"))


def test_live_pin_on_durable_root_settles_as_mark(wA, tmp_path):
    k = wA.kernel_start("local", "python")["kernel_id"]
    r = wA.kernel_exec(k, "open('model.bin','w').write('v1')", timeout=60)
    assert r["rc"] == 0
    pin = wA.run_retain(k, include=["model.bin"])
    assert pin["state"] == "pinned-pending" and pin["moved"] is False
    wA.kernel_exec(k, "open('model.bin','w').write('v2-final')",
                   timeout=60)
    wA.kernel_stop(k)
    row = wA.store.get_retained(k)
    assert row["state"] == "done" and row["method"] == "mark"
    kept = Path(row["location"]) / "model.bin"
    assert kept.read_text() == "v2-final"        # the eventual version
    assert (Path(row["location"]) / ".weft-run.json").exists()


# -- R2: the refusal (topology B) ------------------------------------------

def test_bare_site_refuses_with_levers(wB):
    jid = _run(wB, "echo x > f.txt")
    out = wB.run_retain(jid, include=["f.txt"], background=False)
    assert out["error"] == "retain.no_durable"
    opts = out["hints"]["options"]
    assert "@workspace" in opts["ship_home"]
    assert "durable" in opts["declare"]
    # explicit dest resolves it, per call
    r = wB.run_retain(jid, include=["f.txt"], dest="@workspace",
                      background=False)
    assert r["state"] == "done" and r["moved"] is True
    assert (Path(r["location"]["path"]) / "f.txt").exists()


def test_bare_site_pin_asks_at_retain_time_not_settlement(wB):
    k = wB.kernel_start("local", "python")["kernel_id"]
    out = wB.run_retain(k, include=["later.txt"])
    assert out["error"] == "retain.no_durable"   # asked NOW, not at stop
    wB.kernel_stop(k)


# -- R2: the hop (topology C) ----------------------------------------------

def test_hop_places_site_side_and_forget_deletes_copies(wC, tmp_path):
    jid = _run(wC, "echo stay > model.bin")
    r = wC.run_retain(jid, include=["model.bin"], label="proj9",
                      layout="label", background=False)
    assert r["moved"] is True and r["in_place"] is True
    keep = tmp_path / "keeps" / "runs" / "proj9" / jid
    assert (keep / "model.bin").read_text() == "stay\n"
    assert (keep / ".weft-run.json").exists()
    # the sandbox copy is untouched (hop copies, sandbox lifecycle owns it)
    assert (tmp_path / "site" / "jobs" / jid / "model.bin").exists()
    # forget deletes what PLACE created — the keep tree, not the sandbox
    wC.run_forget(target=jid)
    assert not keep.exists()
    assert (tmp_path / "site" / "jobs" / jid / "model.bin").exists()


def test_legacy_retain_dir_still_hops(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {
        "root": str(tmp_path / "site"), "pixi_source": pixi_bin,
        "retain": {"dir": str(tmp_path / "old-keeps")}})
    w.runner.poll_interval = 0.2
    jid = _run(w, "echo v1 > f.txt")
    r = w.run_retain(jid, include=["f.txt"], background=False)
    assert r["moved"] is True
    assert r["location"]["path"].startswith(str(tmp_path / "old-keeps"))


# -- R3 interlock: marked keeps exempt from an opted-in TTL -----------------

def test_ttl_exempts_marked_keeps(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {
        "root": str(tmp_path / "site"), "pixi_source": pixi_bin,
        "durable": True, "policy": {"run_remains_days": 7}})
    w.runner.poll_interval = 0.2
    kept = _run(w, "echo keep > k.txt")
    junk = _run(w, "echo junk > j.txt")
    w.run_retain(kept, include=["k.txt"], background=False)
    for jid in (kept, junk):
        w.store._write("UPDATE jobs SET updated_at=? WHERE job_id=?",
                       (time.time() - 30 * 86400, jid))
    plan = w.gc_plan("local")["sites"]["local"]
    targets = {x["target"] for x in plan["run_remains"]}
    assert junk in targets and kept not in targets
    w.gc_sweep("local", confirm=True)
    assert (tmp_path / "site" / "jobs" / kept / "k.txt").exists()
    assert not (tmp_path / "site" / "jobs" / junk).exists()
