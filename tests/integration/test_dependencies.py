"""Round E: control-flow chaining (`after=`) and the site notebook."""

import time

import pytest

from weft.api import Weft

pytestmark = pytest.mark.solver


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.3
    return w


def test_three_stage_pipeline_without_polling(w):
    """A → B → C chained by `after`, one submit each, zero agent polling
    between stages; data flows via site-side chaining as usual."""
    env = w.env_ensure({"name": "p", "deps": {"conda": ["xz >=5"]}})["env_id"]
    a = w.task_submit({"command": "sleep 2; echo stage-a > results/a.txt",
                       "env": env, "outputs": ["results/"], "site": "local"})
    b = w.task_submit({"command": "sleep 1; echo stage-b > results/b.txt",
                       "env": env, "outputs": ["results/"], "site": "local",
                       "after": [a["job_id"]]})
    assert b["plan"]["after"]["jobs"] == [a["job_id"]]
    c = w.task_submit({"command": "echo stage-c > results/c.txt",
                       "env": env, "outputs": ["results/"], "site": "local",
                       "after": [b["job_id"]]})
    jc = w.runner.wait(c["job_id"], 900)
    assert jc["state"] == "DONE", jc["error"]
    # order held: A finished before B started, B before C
    ja, jb = w.store.get_job(a["job_id"]), w.store.get_job(b["job_id"])
    assert ja["state"] == "DONE" and jb["state"] == "DONE"
    assert ja["updated_at"] <= jb["updated_at"] <= jc["updated_at"]


def test_failed_dependency_fails_downstream_honestly(w):
    a = w.task_submit({"command": "exit 3", "site": "local"})
    b = w.task_submit({"command": "echo never", "site": "local",
                       "after": [a["job_id"]]})
    jb = w.runner.wait(b["job_id"], 300)
    assert jb["state"] == "FAILED"
    assert jb["error"]["error"] == "task.dep_failed"
    assert jb["error"]["hints"]["dependency"] == a["job_id"]
    assert jb["error"]["hints"]["dependency_state"] == "FAILED"


def test_unknown_dependency_fails_fast(w):
    r = w.task_submit({"command": "true", "site": "local",
                       "after": ["jb_nonexistent"]})
    assert r["error"] == "task.invalid"
    assert "unknown dependency" in r["detail"]


def test_dependency_on_a_done_job_is_a_noop(w):
    a = w.task_submit({"command": "true", "site": "local"})
    assert w.runner.wait(a["job_id"], 300)["state"] == "DONE"
    b = w.task_submit({"command": "true", "site": "local",
                       "after": [a["job_id"]]}, force=True)
    assert w.runner.wait(b["job_id"], 300)["state"] == "DONE"


def test_site_notebook_persists_operational_knowledge(w, tmp_path,
                                                      pixi_bin):
    r = w.site_note("local", "gcc lives in ~/toolchains/gcc-13; "
                             "avoid /tmp for >1GB scratch")
    assert len(r["notes"]) == 1
    w.site_note("local", "quota resets monthly on the 1st")
    desc = w.sites_describe("local")
    notes = [n["note"] for n in desc["site_notebook"]]
    assert "gcc lives in ~/toolchains/gcc-13; avoid /tmp for >1GB scratch" \
        in notes[0]
    assert len(notes) == 2                      # newest last

    # a SECOND controller instance on the same workspace sees them:
    # the notebook is persistent state, not session memory
    w2 = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    notes2 = w2.sites_describe("local")["site_notebook"]
    assert len(notes2) == 2
