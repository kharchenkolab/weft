import threading

import pytest

from weft.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


def test_events_cursor_and_subscribers(store):
    seen = []
    store.subscribe(seen.append)
    c0 = store.emit("job.state", job_id="jb_1", state="PENDING")
    store.emit("job.state", job_id="jb_1", state="RUNNING")
    assert len(seen) == 2
    newer = store.events_since(c0)
    assert len(newer) == 1 and newer[0]["state"] == "RUNNING"
    assert store.events_since(newer[0]["seq"]) == []


def test_broken_subscriber_does_not_break_emit(store):
    def bad(_):
        raise RuntimeError("subscriber bug")
    got = []
    store.subscribe(bad)
    store.subscribe(got.append)
    store.emit("ping")
    assert len(got) == 1


def test_realization_upsert_and_lookup(store):
    store.set_realization("env:v1:abc", "beamlab", "prefix", "/fr/envs/abc", "building")
    store.set_realization("env:v1:abc", "beamlab", "prefix", "/fr/envs/abc", "ready")
    r = store.get_realization("env:v1:abc", "beamlab")
    assert r["state"] == "ready"
    assert store.get_realization("env:v1:abc", "elsewhere") is None


def test_location_demotion_recomputes_plans(store):
    store.put_dataref("dref:aa", "file", 100)
    store.set_location("dref:aa", "hpc", "/scratch/cas/aa")
    assert store.refs_present_at("hpc") == {"dref:aa"}
    store.demote_location("dref:aa", "hpc")
    assert store.refs_present_at("hpc") == set()


def test_job_roundtrip_and_memoization_lookup(store):
    task = {"command": "python scan.py", "env": "env:v1:abc"}
    store.put_job("jb_1", "task:v1:t1", task, "local", "PENDING")
    store.update_job("jb_1", state="DONE", manifest={"exit_code": 0, "outputs": []})
    assert store.get_job("jb_1")["state"] == "DONE"
    assert store.latest_manifest_for_task("task:v1:t1")["exit_code"] == 0
    assert store.latest_manifest_for_task("task:v1:other") is None
    assert store.nonterminal_jobs() == []


def test_concurrent_writers(store):
    def writer(i):
        for k in range(50):
            store.emit("tick", job_id=f"jb_{i}", k=k)
    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.events_since(0, limit=1000)) == 400


def test_audit_log(store):
    store.audit_log("agent", "site.exec", site="hpc", command="df -h", why="check quota")
    tail = store.audit_tail()
    assert tail[-1]["command"] == "df -h" and tail[-1]["why"] == "check quota"
