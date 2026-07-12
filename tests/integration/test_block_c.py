"""Block C: GC plans/sweeps, events retention, metrics collection."""

import time

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin,
                                       "policy": {"gc_idle_days": 0}})
    return w


def _age(w, days=1.0):
    """Backdate every timestamp the GC consults (test-only surgery)."""
    cutoff = time.time() - days * 86400
    with w.store._lock, w.store._conn:
        w.store._conn.execute("UPDATE locations SET verified_at=?", (cutoff,))
        w.store._conn.execute("UPDATE realizations SET updated_at=?", (cutoff,))


def test_gc_plan_and_sweep_with_pinning(w, tmp_path):
    used = tmp_path / "ws" / "used.dat"
    used.write_bytes(b"u" * 50_000)
    orphan = tmp_path / "ws" / "orphan.dat"
    orphan.write_bytes(b"o" * 80_000)
    used_ref = w.data_register("used.dat")["ref"]
    orphan_ref = w.data_register("orphan.dat")["ref"]
    r = w.task_submit({"command": "wc -c < d/u > results/n.txt",
                       "inputs": [{"ref": used_ref, "mount_as": "d/u"}],
                       "outputs": ["results/"], "site": "local"})
    assert w.runner.wait(r["job_id"], 120)["state"] == "DONE"
    _age(w)

    full = w.gc_plan()["sites"]
    ws = full["@workspace"]
    ws_refs = {x["ref"] for x in ws["evictable_refs"]}
    assert orphan_ref in ws_refs        # unreferenced: evictable
    assert used_ref not in ws_refs      # provenance-pinned: protected
    site_p = full["local"]
    site_refs = {x["ref"]: x for x in site_p["evictable_refs"]}
    assert used_ref in site_refs        # remote cache: advisory only
    assert site_refs[used_ref]["pinned_locally"]

    # sweep requires explicit confirmation
    dry = w.gc_sweep("@workspace")
    assert "confirm" in dry["note"]
    swept = w.gc_sweep("@workspace", confirm=True)
    assert swept["evicted_bytes"] >= 80_000
    assert w.cas.kind_of(orphan_ref) is None    # gone
    assert w.cas.kind_of(used_ref) == "file"    # survived
    assert w.gc_sweep("local", confirm=True)["evicted_bytes"] > 0
    # correctness survives eviction: the same task re-runs (re-stages)
    r2 = w.task_submit({"command": "wc -c < d/u > results/n.txt",
                        "inputs": [{"ref": used_ref, "mount_as": "d/u"}],
                        "outputs": ["results/"], "site": "local"},
                       force=True)
    assert w.runner.wait(r2["job_id"], 120)["state"] == "DONE"


@pytest.mark.solver
def test_gc_evicts_idle_realization_and_it_rebuilds(w):
    env = w.env_ensure({"name": "gc-env", "deps": {"conda": ["xz >=5"]}})
    t = {"command": "xz --version > results/v.txt", "env": env["env_id"],
         "outputs": ["results/"], "site": "local"}
    assert w.runner.wait(w.task_submit(t)["job_id"], 900)["state"] == "DONE"
    _age(w)
    p = w.gc_plan("local")["sites"]["local"]
    assert any(x["env_id"] == env["env_id"]
               for x in p["evictable_realizations"])
    w.gc_sweep("local", confirm=True)
    assert w.store.get_realization(env["env_id"], "local")["state"] == "evicted"
    j = w.runner.wait(w.task_submit(t, force=True)["job_id"], 900)
    assert j["state"] == "DONE", j["error"]   # rebuilt transparently


def test_events_retention_keeps_terminal_kinds(w):
    r = w.task_submit({"command": "false", "site": "local"})
    w.runner.wait(r["job_id"], 60)
    n0 = w.store.events_count()
    assert n0 > 0
    with w.store._lock, w.store._conn:   # age everything (test-only)
        w.store._conn.execute("UPDATE events SET ts=ts-90*86400")
    out = w.gc_events(older_than_days=30)
    assert out["pruned"] > 0
    kinds = {e["kind"] for e in w.store.events_since(0, 1000)}
    assert "job.failed" in kinds          # failures survive retention


def test_metrics_recorded_and_summarized(w):
    for v in (10.0, 20.0, 30.0):
        w.store.add_metric("local", "transfer_mbps", v)
    s = w.store.metric_summary("local", "transfer_mbps")
    assert s["n"] == 3 and s["median"] == 20.0 and s["max"] == 30.0
    assert w.store.metric_summary("local", "nothing")["n"] == 0
