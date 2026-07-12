"""Step 4: eviction reclaims GBs and keeps the way back cheap."""

import pytest

from weft.api import Weft

pytestmark = pytest.mark.solver

TINY = {"name": "eviction", "deps": {"conda": ["xz >=5"]}}
TASK = {"command": "xz --version > results/v.txt", "outputs": ["results/"],
        "site": "local"}


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def test_footprint_metadata_and_cache_warm_rebuild(w):
    env = w.env_ensure(TINY)["env_id"]
    assert w.runner.wait(w.task_submit({**TASK, "env": env})["job_id"],
                         900)["state"] == "DONE"

    real = w.env_status(env)["realizations"][0]
    assert real["bytes"] and real["bytes"] > 0        # footprint recorded
    assert real["last_used"] and real["idle_days"] == 0.0   # recency recorded

    fp = w.site_footprint("local")
    assert fp["prefixes_bytes"] > 0 and fp["package_cache_bytes"] > 0
    assert fp["realizations"][0]["env_id"] == env

    out = w.env_evict(env, "local")
    assert out["state"] == "evicted" and out["freed_bytes"] > 0
    assert "seconds, offline" in out["rebuild"]
    assert w.store.get_realization(env, "local")["state"] == "evicted"
    # the prefix is gone from disk; the shared package cache is NOT
    fp2 = w.site_footprint("local")
    assert fp2["prefixes_bytes"] < fp["prefixes_bytes"]
    assert fp2["package_cache_bytes"] == fp["package_cache_bytes"]

    # and it comes straight back (hardlinks from the warm cache)
    j = w.runner.wait(w.task_submit({**TASK, "env": env}, force=True)["job_id"],
                      900)
    assert j["state"] == "DONE", j["error"]
    assert w.env_status(env)["realizations"][0]["state"] == "ready"


def test_archive_rebuilds_without_the_index(w, tmp_path):
    """archive=True keeps the blob on the CONTROLLER: the site reclaims
    ~everything and can still rebuild with no network of its own."""
    env = w.env_ensure(TINY)["env_id"]
    assert w.runner.wait(w.task_submit({**TASK, "env": env})["job_id"],
                         900)["state"] == "DONE"
    out = w.env_evict(env, "local", archive=True)
    assert out["archive_ref"].startswith("dref:")
    assert "offline from the controller" in out["rebuild"]

    # now make the site air-gapped AND blow away its package cache: only the
    # controller's archive can bring the env back
    w.gc_packages("local", confirm=True)
    w.store.set_capabilities("local", {
        **(w.store.get_site("local")["capabilities"]), "internet": False})
    w.adapters["local"] = w._make_adapter("local", "local",
                                          w.store.get_site("local")["config"])

    j = w.runner.wait(w.task_submit({**TASK, "env": env}, force=True)["job_id"],
                      1800)
    assert j["state"] == "DONE", j["error"]
    assert w.store.get_realization(env, "local")["strategy"] == "packed"


def test_gc_packages_warns_before_confirming(w):
    env = w.env_ensure(TINY)["env_id"]
    w.runner.wait(w.task_submit({**TASK, "env": env})["job_id"], 900)
    dry = w.gc_packages("local")
    assert dry["cache_bytes"] > 0 and "confirm=true" in dry["note"]
    assert "need index access" in dry["note"]
    assert w.gc_packages("local", confirm=True)["freed_bytes"] > 0
