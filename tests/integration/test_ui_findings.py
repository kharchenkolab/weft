"""Round 12: fixes for what weft-ui found in the wild (misc/from-weft-ui.md).

1. Concurrent array elements staging one CAS object must not race
   (SameFileError surfaced as a bogus non-retryable state.conflict).
2. Unexpected internal exceptions get their OWN error code with a
   traceback, not a state.conflict costume.
3. array_retry marks the replaced row (superseded_by) so consumers can
   fold history instead of seeing mystery duplicates.
"""

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def _wait_group(w, group, n, timeout=180):
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        c = w.store.group_counts(group)
        if c["total"] >= n and all(
                j["state"] in ("DONE", "FAILED", "CANCELLED")
                for j in w.store.jobs_in_group(group)):
            return c
        time.sleep(0.3)
    raise AssertionError(f"group did not settle: {w.store.group_counts(group)}")


def test_array_elements_share_one_input_ref(w, tmp_path):
    """The wild repro: 24 elements all mounting the same ref on a fresh
    site — every element stages the identical blob concurrently."""
    data = tmp_path / "ws" / "code.bin"
    data.write_bytes(b"shared" * 4096)
    ref = w.data_register("code.bin")["ref"]
    r = w.task_submit({
        "command": "wc -c < d/in > results/n.txt",
        "inputs": [{"ref": ref, "mount_as": "d/in"}],
        "outputs": ["results/"], "array": 24, "site": "local"})
    counts = _wait_group(w, r["group"], 24)
    assert counts["done"] == 24, counts


def test_internal_exception_is_internal_error(w, monkeypatch):
    """A bug in the drive path must surface as internal.error with a
    traceback tail — never as state.conflict wearing a 'concurrent
    operation' meaning."""
    def boom(job_id):
        raise RuntimeError("simulated internal bug")
    monkeypatch.setattr(w.runner, "_drive_inner", boom)
    r = w.task_submit({"command": "true", "site": "local"})
    job = w.runner.wait(r["job_id"], 60)
    assert job["state"] == "FAILED"
    err = job["error"]
    assert err["error"] == "internal.error"
    assert "simulated internal bug" in err["detail"]
    assert "RuntimeError" in err["hints"]["traceback_tail"]
    assert "concurrent" not in err["meaning"]


def test_array_retry_marks_superseded(w):
    r = w.task_submit({
        "command": 'test "$WEFT_ARRAY_INDEX" != 2 || exit 3',
        "array": 4, "site": "local"})
    group = r["group"]
    counts = _wait_group(w, group, 4)
    assert counts["failed"] == 1, counts
    failed = w.store.jobs_in_group(group, state="FAILED")[0]

    out = w.array_retry(group, command_override="true")
    assert out["retried"][0]["superseded"] == failed["job_id"]
    new_id = out["retried"][0]["job_id"]
    assert w.runner.wait(new_id, 60)["state"] == "DONE"

    # the old row: out of the group's counts, but linked to its successor
    old = w.store.get_job(failed["job_id"])
    assert old["array_group"] is None
    assert old["superseded_by"] == new_id
    # and jobs_where surfaces the linkage for UIs to fold
    loose = [j for j in w.store.jobs_where(state="FAILED")
             if j["job_id"] == failed["job_id"]]
    assert loose and loose[0]["superseded_by"] == new_id
    assert w.store.group_counts(group)["total"] == 4
