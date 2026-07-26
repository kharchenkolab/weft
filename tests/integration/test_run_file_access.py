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


# ── ranged read (run_file_read_range): the TRANSPORT tier ──────────────────
# A byte range served without moving the whole file; the arm behind
# HTTP Range serving for remote chunked stores.

def _mk_run(w, command):
    jid = w.task_submit({"command": command, "site": "local"})["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    return jid


def test_range_read_offsets_and_eof(w):
    # 26 bytes: a..z — every degenerate offset/length in one file
    jid = _mk_run(w, "printf abcdefghijklmnopqrstuvwxyz > alpha.txt")

    whole = w.run_file_read_range(jid, "alpha.txt")
    assert base64.b64decode(whole["bytes_b64"]) == b"abcdefghijklmnopqrstuvwxyz"
    assert (whole["offset"], whole["nbytes"], whole["size"]) == (0, 26, 26)
    assert whole["eof"] is True and whole["capped"] is False
    assert whole["at"] == "sandbox"

    # a middle window: bytes [10, 15)
    mid = w.run_file_read_range(jid, "alpha.txt", offset=10, length=5)
    assert base64.b64decode(mid["bytes_b64"]) == b"klmno"
    assert (mid["offset"], mid["nbytes"], mid["size"]) == (10, 5, 26)
    assert mid["eof"] is False

    # offset 0, short length
    head = w.run_file_read_range(jid, "alpha.txt", offset=0, length=3)
    assert base64.b64decode(head["bytes_b64"]) == b"abc" and head["eof"] is False

    # offset == size: empty payload, eof True (not an error)
    at_end = w.run_file_read_range(jid, "alpha.txt", offset=26, length=8)
    assert at_end["nbytes"] == 0 and at_end["eof"] is True
    assert at_end["size"] == 26 and "error" not in at_end

    # offset > size: same least-surprise shape (416 is the caller's call)
    past = w.run_file_read_range(jid, "alpha.txt", offset=99, length=8)
    assert past["nbytes"] == 0 and past["eof"] is True and past["size"] == 26

    # length 0: empty payload mid-file, not eof
    zero = w.run_file_read_range(jid, "alpha.txt", offset=5, length=0)
    assert zero["nbytes"] == 0 and zero["eof"] is False

    # a tail read that reaches EOF exactly
    tail = w.run_file_read_range(jid, "alpha.txt", offset=20, length=100)
    assert base64.b64decode(tail["bytes_b64"]) == b"uvwxyz" and tail["eof"] is True


def test_range_read_binary_survives(w):
    # every byte value 0..255 — proves the payload is NOT text-mangled
    jid = _mk_run(
        w, r"""python3 -c 'open("blob.bin","wb").write(bytes(range(256)))'""")
    got = w.run_file_read_range(jid, "blob.bin", offset=0, length=256)
    assert base64.b64decode(got["bytes_b64"]) == bytes(range(256))
    assert got["nbytes"] == 256 and got["size"] == 256 and got["eof"] is True
    # a range that starts inside the high (non-utf8) bytes
    hi = w.run_file_read_range(jid, "blob.bin", offset=250, length=4)
    assert base64.b64decode(hi["bytes_b64"]) == bytes([250, 251, 252, 253])


def test_range_read_cap_clamps_both_sides(w, monkeypatch):
    # ARMED: shrink the cap so length>cap is REACHED; a run that never
    # crosses the cap would leave capped False and fail here.
    monkeypatch.setattr(type(w.retains), "_RANGE_READ_CAP", 4)
    jid = _mk_run(w, "printf 0123456789 > nums.txt")   # 10 bytes
    over = w.run_file_read_range(jid, "nums.txt", offset=0, length=1000)
    assert over["capped"] is True                      # request exceeded cap
    assert over["nbytes"] == 4                          # controller clamp
    assert base64.b64decode(over["bytes_b64"]) == b"0123"
    assert over["eof"] is False                         # more remains
    # length within cap: no clamp
    under = w.run_file_read_range(jid, "nums.txt", offset=0, length=3)
    assert under["capped"] is False and under["nbytes"] == 3
    # length=None reads up to the cap
    dflt = w.run_file_read_range(jid, "nums.txt")
    assert dflt["nbytes"] == 4 and dflt["capped"] is False


def test_range_read_missing_and_swept(w):
    jid = _mk_run(w, "echo present > here.txt")
    miss = w.run_file_read_range(jid, "gone.txt")
    assert miss["error"] == "data.missing"
    # a swept sandbox reads as missing, same as run_file_read
    w.run_discard(jid)
    swept = w.run_file_read_range(jid, "here.txt")
    assert swept["error"] == "data.missing"


def test_range_read_traversal_refused(w, tmp_path):
    (tmp_path / "secret.txt").write_text("not yours")
    jid = _mk_run(w, "true")
    for rel in ("../../../secret.txt", "../" * 8 + "etc/passwd",
                "ok/../../escape"):
        out = w.run_file_read_range(jid, rel, offset=0, length=16)
        assert out["error"] == "task.invalid" and "escapes" in out["detail"]


def test_range_read_intake_refusals(w):
    jid = _mk_run(w, "echo x > f.txt")
    assert w.run_file_read_range(jid, "f.txt", offset=-1)["error"] == \
        "task.invalid"
    assert w.run_file_read_range(jid, "f.txt", length=-5)["error"] == \
        "task.invalid"


def test_range_read_follows_the_keep(w):
    # precedence matches the singular verbs: a moved keep answers for a
    # swept sandbox, and the bytes are still correct.
    jid = _mk_run(w, "printf keptbytes > keep.txt")
    out = w.run_retain(jid, include=["keep.txt"], background=False,
                       dest="@workspace")
    assert out["state"] == "done", out
    w.run_discard(jid)
    got = w.run_file_read_range(jid, "keep.txt", offset=3, length=4)
    assert got["at"] == "retained"
    assert base64.b64decode(got["bytes_b64"]) == b"tbyt"
    assert got["size"] == 9


# ── the shim read-from lane directly: new arms opt-in, log lane frozen ─────

def test_shim_read_from_log_lane_is_byte_identical(w, tmp_path):
    """The log-follow lane (no --root, no --base64) still returns RAW
    bytes with the same offset/max semantics — the streaming callers
    (task_logs, kernel_peek) depend on this exact behavior."""
    ad = w.adapters["local"]
    p = tmp_path / "log.txt"
    p.write_text("line-a\nline-b\nline-c\n")
    r = ad.shim(["read-from", "--file", str(p), "--offset", "7", "--max", "6"])
    assert r.rc == 0 and r.out == "line-b"          # raw, not base64
    # remote-side --max is honored: never more than the ceiling asked
    r = ad.shim(["read-from", "--file", str(p), "--offset", "0", "--max", "4"])
    assert r.out == "line" and len(r.out) == 4


def test_shim_read_from_root_rejects_escape(w, tmp_path):
    """Remote-side containment: --root refuses an out-of-tree file and a
    '..' component with rc 3 — the mirror of the controller check."""
    ad = w.adapters["local"]
    root = tmp_path / "tree"
    root.mkdir()
    (root / "ok.txt").write_text("inside")
    secret = tmp_path / "secret.txt"
    secret.write_text("outside")
    # in-tree, base64 arm: reads and encodes
    r = ad.shim(["read-from", "--file", str(root / "ok.txt"),
                 "--root", str(root), "--offset", "0", "--max", "64",
                 "--base64"])
    assert r.rc == 0 and base64.b64decode("".join(r.out.split())) == b"inside"
    # absolute path outside root: rejected
    r = ad.shim(["read-from", "--file", str(secret), "--root", str(root),
                 "--offset", "0", "--max", "64"])
    assert r.rc == 3
    # a '..' component that would climb out: rejected
    r = ad.shim(["read-from", "--file", f"{root}/../secret.txt",
                 "--root", str(root), "--offset", "0", "--max", "64"])
    assert r.rc == 3
