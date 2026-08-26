"""Site evidence-log lifecycle (T1): every long-lane op persists its
full output under <root>/logs/ (the L2 contract), so the directory
grows without bound. Contracts pinned here: the footprint verb SEES
logs (honest numbers — an invisible growing dir falsifies the site's
byte report); gc_sweep's plan lists stale logs by the
logs_max_age_days policy (default 30; 0 opts out); confirm deletes
only the stale ones and emits the receipt."""

import os
import time

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def _lay_logs(tmp_path, old_n=2, young_n=1):
    logs = tmp_path / "site" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    old_t = time.time() - 40 * 86400
    for i in range(old_n):
        p = logs / f"stale-{i}.log"
        p.write_text("x" * 100)
        os.utime(p, (old_t, old_t))
    for i in range(young_n):
        (logs / f"fresh-{i}.log").write_text("y" * 50)
    return logs


def test_footprint_sees_logs(w, tmp_path):
    _lay_logs(tmp_path)
    fp = w.site_footprint("local")
    assert fp["logs_bytes"] >= 250


def test_plan_lists_stale_logs_by_policy(w, tmp_path):
    _lay_logs(tmp_path, old_n=2, young_n=1)
    p = w.gc_sweep("local")           # dry run returns the plan
    sl = p["stale_logs"]
    assert sl["policy_days"] == 30
    assert sl["count"] == 2
    assert sl["bytes"] == 200


def test_policy_zero_opts_out(w, tmp_path):
    _lay_logs(tmp_path)
    row = w.store.get_site("local")
    cfg = dict(row["config"])
    cfg["policy"] = {**(cfg.get("policy") or {}), "logs_max_age_days": 0}
    w.store.put_site("local", row["kind"], cfg)
    p = w.gc_sweep("local")
    assert p["stale_logs"]["count"] == 0
    assert p["stale_logs"]["policy_days"] == 0


def test_sweep_deletes_stale_keeps_fresh_and_emits(w, tmp_path):
    logs = _lay_logs(tmp_path, old_n=2, young_n=1)
    w.gc_sweep("local", confirm=True)
    left = sorted(f.name for f in logs.iterdir())
    assert left == ["fresh-0.log"]
    ev = [e for e in w.store.events_since(0, limit=500)
          if e["kind"] == "gc.logs_swept"]
    assert ev and ev[-1]["count"] == 2 and ev[-1]["bytes"] == 200
