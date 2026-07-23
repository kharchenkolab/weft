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

# ── batched stats/inventories (aba store/NFS note): O(1), not O(N) ─────────

def test_batched_stat_is_one_invocation_and_o1_queries(w, monkeypatch):
    r = w.task_submit({"command": "echo a > f1 && printf bb > f2",
                       "site": "local"})
    jid = r["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    ad = w.adapters["local"]
    cmds, queries = [], []
    orig_cmd, orig_rows = ad.run_cmd, w.store._rows
    monkeypatch.setattr(
        ad, "run_cmd",
        lambda s, timeout=120.0: (cmds.append(s),
                                  orig_cmd(s, timeout=timeout))[1])
    monkeypatch.setattr(
        w.store, "_rows",
        lambda sql, params=(): (queries.append(sql),
                                orig_rows(sql, params))[1])
    out = w.run_file_stat(jid, rels=["f1", "f2", "nope.txt"])
    assert len(cmds) == 1, cmds              # ONE shell invocation
    assert len(queries) <= 3, queries        # O(1) store reads, not 2N
    files = out["files"]
    assert files["f1"]["exists"] and files["f1"]["bytes"] == 2
    assert files["f1"]["at"] == "sandbox"
    assert files["f2"]["bytes"] == 2
    assert files["nope.txt"]["exists"] is False


def test_batched_stat_follows_the_keep(w):
    """Precedence inside the batch matches the singular verb: a moved
    keep answers for a swept sandbox (the fast-path trap the consumer
    was warned about — a sandbox-only stat lies here)."""
    r = w.task_submit({"command": "echo kept > keep.txt", "site": "local"})
    jid = r["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    out = w.run_retain(jid, include=["keep.txt"], background=False,
                       dest="@workspace")
    assert out["state"] == "done", out
    w.run_discard(jid)
    got = w.run_file_stat(jid, rels=["keep.txt", "gone.txt"])
    assert got["files"]["keep.txt"]["exists"] is True
    assert got["files"]["keep.txt"]["at"] == "retained"
    assert got["files"]["gone.txt"]["exists"] is False


def test_batched_stat_intake_refusals(w):
    r = w.task_submit({"command": "true", "site": "local"})
    jid = r["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    assert w.run_file_stat(jid)["error"] == "task.invalid"        # neither
    assert w.run_file_stat(jid, rel="a", rels=["b"])["error"] == \
        "task.invalid"                                            # both
    assert w.run_file_stat(jid, rels=[])["error"] == "task.invalid"
    assert w.run_file_stat(jid, rels=["ok", 7])["error"] == "task.invalid"
    # one escaping entry refuses the WHOLE call, naming it
    out = w.run_file_stat(jid, rels=["fine.txt", "../escape"])
    assert out["error"] == "task.invalid" and "escape" in out["detail"]
    over = ["f%d" % i for i in range(1001)]
    out = w.run_file_stat(jid, rels=over)
    assert out["error"] == "task.invalid" and "chunk" in out["detail"]


def test_inventory_batches_targets_with_per_entry_errors(w):
    r1 = w.task_submit({"command": "echo x > out1.txt", "site": "local"})
    r2 = w.task_submit({"command": "echo y > out2.txt", "site": "local"})
    for r in (r1, r2):
        assert w.runner.wait(r["job_id"], 120)["state"] == "DONE"
    out = w.run_inventory(targets=[r1["job_id"], r2["job_id"],
                                   "job_nonexistent"])
    inv = out["inventories"]
    assert "out1.txt" in {e["path"] for e in
                          inv[r1["job_id"]]["entries"]}
    assert "out2.txt" in {e["path"] for e in
                          inv[r2["job_id"]]["entries"]}
    # the singular verb's typed error rides the batch VERBATIM (an
    # unknown run and a receiptless run both answer data.missing —
    # the singular contract, carried through)
    assert inv["job_nonexistent"]["error"] == "data.missing"
    # intake refusals: live is per-run site work; no mixing target=
    assert w.run_inventory(targets=[r1["job_id"]],
                           live=True)["error"] == "task.invalid"
    assert w.run_inventory(target=r1["job_id"],
                           targets=[r2["job_id"]])["error"] == \
        "task.invalid"
    assert w.run_inventory()["error"] == "task.invalid"
