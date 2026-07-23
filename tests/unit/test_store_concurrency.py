"""Store/NFS round (aba field note): reads ride a per-thread reader
lane and never take the write lock — a UI poll must not stall behind an
agent turn's writes. The pins here are DETERMINISTIC (lock held by the
test, not timing races): on the old single-connection store every one
of them deadlocks or errors."""

import sqlite3
import threading
import time

import pytest

from weft.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    s.put_env("env:v1:" + "a" * 64, "spec_x", {"platforms": {}},
              "lock: 1", "[project]", ["any"])
    yield s
    s.close()


def test_read_completes_while_write_lock_is_held(store):
    """THE property: the write lock gates writers only. Held lock +
    completed read = readers are off the lock, structurally."""
    got = {}

    def read():
        got["env"] = store.get_env("env:v1:" + "a" * 64)

    with store._lock:                       # a long write in progress
        t = threading.Thread(target=read)
        t.start()
        t.join(timeout=5.0)
        assert not t.is_alive(), "read blocked behind the write lock"
    assert got["env"]["env_id"].endswith("a" * 64)


def test_read_sees_data_committed_mid_hold(store):
    """The reader lane answers from COMMITTED WAL state: a write that
    committed before the read is visible even while the next write
    transaction holds the lock."""
    store._write("UPDATE envs SET weakly_reproducible=1")
    got = {}
    with store._lock:
        t = threading.Thread(target=lambda: got.update(
            row=store.get_env("env:v1:" + "a" * 64)))
        t.start(); t.join(timeout=5.0)
        assert not t.is_alive()
    assert got["row"]["weakly_reproducible"] == 1


def test_reader_lane_refuses_writes(store):
    """query_only guards the lane: a write smuggled through _rows is a
    coding bug and must fail loudly, never silently bypass the
    single-writer discipline."""
    with pytest.raises(sqlite3.OperationalError):
        store._rows("UPDATE envs SET weakly_reproducible=2")


def test_busy_timeout_and_wal_bounds_set_everywhere(store):
    store._read_conn()          # materialize this thread's reader
    for conn in (store._conn, store._read_conn()):
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert store._conn.execute(
        "PRAGMA journal_size_limit").fetchone()[0] == 8388608
    assert store._conn.execute(
        "PRAGMA journal_mode").fetchone()[0] == "wal"


def test_cross_process_write_waits_out_contention(store, tmp_path):
    """busy_timeout: a second store (second PROCESS shape) holding the
    write lock makes this store WAIT, not error — the CLI-beside-
    controller case that used to eat SQLITE_BUSY raw."""
    other = Store(tmp_path / "state.db")
    try:
        other._conn.execute("BEGIN IMMEDIATE")     # holds the db lock
        done = {}

        def write():
            store._write("UPDATE envs SET weakly_reproducible=3")
            done["at"] = time.monotonic()

        t = threading.Thread(target=write)
        t0 = time.monotonic()
        t.start()
        time.sleep(0.4)
        assert t.is_alive(), "write should be WAITING on the other lock"
        other._conn.execute("COMMIT")
        t.join(timeout=5.0)
        assert not t.is_alive()
        assert done["at"] - t0 >= 0.35      # it waited, then succeeded
        assert store._rows(
            "SELECT weakly_reproducible FROM envs")[0][0] == 3
    finally:
        other.close()


def test_reader_connections_are_per_thread_and_persistent(store):
    conns = {}

    def grab(key):
        conns[key] = (store._read_conn(), store._read_conn())

    a = threading.Thread(target=grab, args=("t1",))
    b = threading.Thread(target=grab, args=("t2",))
    a.start(); b.start(); a.join(); b.join()
    assert conns["t1"][0] is conns["t1"][1]      # persistent per thread
    assert conns["t1"][0] is not conns["t2"][0]  # not shared across


def test_many_readers_during_writes_smoke(store):
    """Machine-cadence smoke: 4 reader threads at full speed while a
    writer streams updates — every read succeeds, no SQLITE_BUSY, no
    cross-thread cursor errors."""
    stop = threading.Event()
    errors = []

    def reader():
        try:
            while not stop.is_set():
                assert store.get_env("env:v1:" + "a" * 64)
        except Exception as e:            # noqa: BLE001 — collect all
            errors.append(e)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for i in range(200):
        store._write("UPDATE envs SET weakly_reproducible=?", (i,))
    stop.set()
    for t in threads:
        t.join(timeout=5.0)
    assert not errors, errors


# ── state_dir lever: state.db off-NFS, CAS stays in the workspace ──────────

def test_state_dir_relocates_state_db(tmp_path, pixi_bin):
    from weft.api import Weft
    fast = tmp_path / "fastdisk"
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin, state_dir=fast)
    assert (fast / "state.db").exists()
    assert not (tmp_path / "ws" / ".weft" / "state.db").exists()
    assert (tmp_path / "ws" / ".weft" / "cas").exists()   # CAS stays put
    # reopen: same workspace, same state_dir — fine
    w2 = Weft(tmp_path / "ws", pixi_bin=pixi_bin, state_dir=fast)
    assert w2.store.path == w.store.path


def test_state_dir_refuses_a_second_workspace(tmp_path, pixi_bin):
    """Two workspaces on one state_dir would silently merge their
    state — refused at construction, naming the owner."""
    from weft.api import Weft
    from weft.errors import WeftError
    fast = tmp_path / "fastdisk"
    Weft(tmp_path / "ws-a", pixi_bin=pixi_bin, state_dir=fast)
    with pytest.raises(WeftError) as ei:
        Weft(tmp_path / "ws-b", pixi_bin=pixi_bin, state_dir=fast)
    assert ei.value.code == "task.invalid"
    assert "ws-a" in str(ei.value.hints.get("owner"))


def test_state_dir_env_var(tmp_path, pixi_bin, monkeypatch):
    from weft.api import Weft
    fast = tmp_path / "envdisk"
    monkeypatch.setenv("WEFT_STATE_DIR", str(fast))
    Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    assert (fast / "state.db").exists()


def test_dead_threads_do_not_leak_reader_connections(store):
    """The registry holds WEAK refs: a short-lived thread's reader
    connection is GC-eligible once the thread dies (weft spawns
    driver/heartbeat threads per job — a strong registry leaks one fd
    per thread for the controller's lifetime)."""
    import gc

    def touch():
        store._read_conn()

    for _ in range(8):
        t = threading.Thread(target=touch)
        t.start(); t.join()
    gc.collect()
    with store._readers_guard:
        live = [r for r in store._readers if r() is not None]
    assert len(live) <= 1, f"{len(live)} reader conns survive dead threads"
