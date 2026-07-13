"""G5: hierarchical array triage — failure buckets by log signature,
paged element listing, linked retries."""

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.3
    return w


def _run_group(w, command, n):
    r = w.task_submit({"command": command, "site": "local", "array": n})
    for sub in r["jobs"]:
        w.runner.wait(sub["job_id"], 600)
    return r["group"]


def test_failure_buckets_cluster_by_mode(w):
    """Two distinct failure modes in one sweep → two buckets, biggest
    first, sample indices pointing into each."""
    group = _run_group(
        w,
        'case "$WEFT_ARRAY_INDEX" in '
        '1|3|5) echo "Traceback (most recent call last):" >&2; exit 1;; '
        '2) exit 137;; '
        '*) true;; esac', 8)
    st = w.array_status(group)
    assert st["failed"] == 4 and st["done"] == 4
    buckets = st["failure_buckets"]
    assert len(buckets) == 2
    assert buckets[0]["count"] == 3            # the traceback trio
    assert set(buckets[0]["sample_indices"]) == {1, 3, 5}
    assert buckets[1]["count"] == 1
    assert buckets[1]["sample_indices"] == [2]
    assert buckets[0]["signature"] != buckets[1]["signature"]


def test_elements_page_and_filter(w):
    group = _run_group(
        w, 'test "$WEFT_ARRAY_INDEX" -lt 3 && exit 4; true', 10)
    page = w.array_elements(group, offset=0, limit=4)
    assert [e["index"] for e in page["elements"]] == [0, 1, 2, 3]
    page2 = w.array_elements(group, offset=8, limit=4)
    assert [e["index"] for e in page2["elements"]] == [8, 9]
    failed = w.array_elements(group, state="FAILED", limit=100)
    assert [e["index"] for e in failed["elements"]] == [0, 1, 2]


def test_large_group_status_stays_compact(w, monkeypatch):
    monkeypatch.setattr(Weft, "_ARRAY_INLINE_CAP", 5)
    group = _run_group(w, "true", 8)
    st = w.array_status(group)
    assert "elements" not in st
    assert "array_elements" in st["note"]
    assert st["done"] == 8
