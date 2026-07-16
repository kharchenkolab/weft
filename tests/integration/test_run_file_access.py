"""Sandbox preview reads (aba Files panel): stat for in-sandbox vs
swept precision, capped reads for previews, traversal confined."""

import base64

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def test_stat_and_read_live_kernel_file(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    r = w.kernel_exec(k, "open('preview.csv','w').write('a,b\\n1,2\\n')",
                      timeout=60)
    assert r["rc"] == 0
    st = w.run_file_stat(k, "preview.csv")
    assert st["exists"] and st["bytes"] == 8 and st["mtime"] > 0
    got = w.run_file_read(k, "preview.csv")
    assert base64.b64decode(got["bytes_b64"]) == b"a,b\n1,2\n"
    assert got["truncated"] is False
    # capped read is honest about truncation
    got = w.run_file_read(k, "preview.csv", max_bytes=3)
    assert base64.b64decode(got["bytes_b64"]) == b"a,b"
    assert got["truncated"] is True and got["bytes_total"] == 8
    w.kernel_stop(k)


def test_swept_vs_present_distinction(w):
    r = w.task_submit({"command": "echo x > made.txt", "site": "local"})
    jid = r["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    assert w.run_file_stat(jid, "made.txt")["exists"] is True
    w.run_discard(jid)
    # inventory says it EXISTED; stat says it's gone — the panel's
    # "cleared" state
    assert "made.txt" in {e["path"] for e in
                          w.run_inventory(jid)["entries"]}
    assert w.run_file_stat(jid, "made.txt")["exists"] is False
    miss = w.run_file_read(jid, "made.txt")
    assert miss["error"] == "data.missing"
    assert "swept" in miss["hints"]["note"]


def test_traversal_is_refused_not_resolved(w, tmp_path):
    (tmp_path / "secret.txt").write_text("not yours")
    r = w.task_submit({"command": "true", "site": "local"})
    jid = r["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    for rel in ("../../../secret.txt", "../" * 8 + "etc/passwd",
                "ok/../../escape"):
        out = w.run_file_read(jid, rel)
        assert out["error"] == "task.invalid"
        assert "escapes" in out["detail"]
        out = w.run_file_stat(jid, rel)
        assert out["error"] == "task.invalid"


def test_read_hard_cap_holds(w):
    r = w.task_submit({"command": "dd if=/dev/zero of=big.bin bs=1m "
                                  "count=12 2>/dev/null", "site": "local"})
    jid = r["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    got = w.run_file_read(jid, "big.bin", max_bytes=1 << 30)  # asks 1GB
    assert len(base64.b64decode(got["bytes_b64"])) == 8 << 20  # gets 8MB
    assert got["truncated"] is True