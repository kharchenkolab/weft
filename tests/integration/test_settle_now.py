"""settle="now" (aba2 live-durability ask): capture CURRENT bytes of a
live run immediately — the caller's "these are done" assertion replaces
block attribution, and weft compensates with a digest ledger (per-file
sha256 in the sidecar) plus a post-capture source re-stat (a tear is
flagged changed_during_capture + retain.unstable, never silent). No
residual pins: unmatched literal asks come back as `not_yet`. The
record model stays one row per target: re-snapshot is a new capture; a
PIN replacing a settled snapshot is refused (retain.keep_exists)
because settlement would clobber the banked bytes."""

import hashlib
import json
from pathlib import Path

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def _events(w, kind):
    return [e for e in w.events_poll(0, 500)["events"] if e["kind"] == kind]


def test_live_root_file_snapshot_lands_now(w):
    """The motivating case: a root file no block can claim, on a kernel
    that keeps living — the bytes reach the keep NOW, digested, and
    survive the scratch."""
    k = w.kernel_start("local", "python")["kernel_id"]
    r = w.kernel_exec(k, "open('results.rds', 'w').write('model-v1')",
                      timeout=60)
    assert r["rc"] == 0
    out = w.run_retain(k, include=["results.rds"], dest="@workspace",
                       background=False, settle="now")
    assert out["state"] == "done" and out["settle"] == "now", out
    dest = Path(out["location"]["path"])
    assert (dest / "results.rds").read_text() == "model-v1"
    sidecar = json.loads((dest / ".weft-run.json").read_text())
    assert sidecar["settle"] == "now"
    (entry,) = sidecar["files"]
    assert entry["sha256"] == hashlib.sha256(b"model-v1").hexdigest()
    assert "changed_during_capture" not in entry
    # the kernel LIVES on
    assert w.kernel_exec(k, "print('still here')", timeout=60)["rc"] == 0
    # durability: the scratch copy dies, the (run, relpath) key answers
    # from the keep
    assert w.kernel_exec(k, "import os; os.remove('results.rds')",
                         timeout=60)["rc"] == 0
    st = w.run_file_stat(k, rel="results.rds")
    assert st["exists"] and st["at"] == "retained"
    w.kernel_stop(k)
    assert w.store.get_retained(k)["state"] == "done"   # settle: no-op


def test_not_yet_reported_never_pinned(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    assert w.kernel_exec(k, "open('exists.txt', 'w').write('x')",
                         timeout=60)["rc"] == 0
    out = w.run_retain(k, include=["exists.txt", "never.txt"],
                       dest="@workspace", background=False, settle="now")
    assert out["state"] == "done"
    assert out["not_yet"] == ["never.txt"]
    assert w.store.get_retained(k)["state"] == "done"   # NOT a pin
    ev = _events(w, "retain.done")
    assert ev and ev[-1].get("not_yet") == ["never.txt"]
    w.kernel_stop(k)


def test_snapshot_nothing_matched_refuses(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    out = w.run_retain(k, include=["ghost.txt"], dest="@workspace",
                       background=False, settle="now")
    assert out["error"] == "data.missing"
    assert "exist now" in str(out["hints"].get("note"))
    assert w.store.get_retained(k) is None              # no record minted
    w.kernel_stop(k)


def test_settle_vocab_folds_and_terminal_uniformity(w):
    jid = w.task_submit({"command": "echo done > out.txt",
                         "site": "local"})["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    # unknown word refuses with the vocabulary
    bad = w.run_retain(jid, include=["out.txt"], dest="@workspace",
                       settle="later")
    assert bad["error"] == "task.invalid"
    assert bad["hints"]["known"] == ["end", "now"]
    # case folds; terminal runs get the SAME snapshot semantics
    # (digest ledger + settle field) — the verb means one thing
    out = w.run_retain(jid, include=["out.txt"], dest="@workspace",
                       background=False, settle="NOW")
    assert out["state"] == "done" and out["settle"] == "now"
    sidecar = json.loads(
        (Path(out["location"]["path"]) / ".weft-run.json").read_text())
    assert sidecar["files"][0]["sha256"] == \
        hashlib.sha256(b"done\n").hexdigest()


def test_resnapshot_is_new_capture_not_error(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    assert w.kernel_exec(k, "open('f.txt', 'w').write('v1')",
                         timeout=60)["rc"] == 0
    out1 = w.run_retain(k, include=["f.txt"], dest="@workspace",
                        background=False, settle="now")
    assert out1["state"] == "done"
    assert w.kernel_exec(k, "open('f.txt', 'w').write('v2-longer')",
                         timeout=60)["rc"] == 0
    out2 = w.run_retain(k, include=["f.txt"], dest="@workspace",
                        background=False, settle="now")
    assert out2["state"] == "done"                      # new capture
    dest = Path(out2["location"]["path"])
    assert (dest / "f.txt").read_text() == "v2-longer"
    sidecar = json.loads((dest / ".weft-run.json").read_text())
    assert sidecar["files"][0]["sha256"] == \
        hashlib.sha256(b"v2-longer").hexdigest()        # drift, ledgered
    w.kernel_stop(k)


def test_pin_over_snapshot_refused_with_levers(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    assert w.kernel_exec(k, "open('a.txt', 'w').write('banked')",
                         timeout=60)["rc"] == 0
    assert w.run_retain(k, include=["a.txt"], dest="@workspace",
                        background=False, settle="now")["state"] == "done"
    # a pin would REPLACE the snapshot row; settlement would then
    # overwrite the banked bytes — refused, with both levers named
    bad = w.run_retain(k, include=["b.txt"], dest="@workspace",
                       background=False)
    assert bad["error"] == "retain.keep_exists"
    assert "settle='now'" in str(bad["hints"]["levers"]["recapture"])
    assert "run_forget" in str(bad["hints"]["levers"]["release"])
    # run_forget releases the record; the pin lane reopens
    w.run_forget(target=k)
    pin = w.run_retain(k, include=["b.txt"], dest="@workspace",
                       background=False)
    assert pin["state"] == "pinned-pending"
    w.kernel_stop(k)


def test_capture_in_flight_refuses_second_retain(w):
    jid = w.task_submit({"command": "echo x > f.txt",
                         "site": "local"})["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    # simulate a live capture thread exactly as the guard sees one
    assert w.retains._claim(jid)
    out = w.run_retain(jid, include=["f.txt"], dest="@workspace",
                       background=False)
    assert out["error"] == "state.conflict" and out["retryable"]
    w.retains._release(jid)
    out = w.run_retain(jid, include=["f.txt"], dest="@workspace",
                       background=False)
    assert out["state"] == "done"


def test_writer_during_capture_flagged_unstable(w, monkeypatch):
    """Hostile-ambient case: the kernel keeps writing the file the
    caller swore was done. The keep lands (point-in-time bytes), the
    entry is flagged, the event fires — never silent."""
    k = w.kernel_start("local", "python")["kernel_id"]
    assert w.kernel_exec(k, "open('grow.txt', 'w').write('seed')",
                         timeout=60)["rc"] == 0
    jd = Path(w.adapters["local"].path(w.store.get_kernel(k)["jobdir"]))
    orig = w.retains._place

    def tampering(*a, **kw):
        method = orig(*a, **kw)
        (jd / "grow.txt").write_text("seed-and-then-some")
        return method

    monkeypatch.setattr(w.retains, "_place", tampering)
    out = w.run_retain(k, include=["grow.txt"], dest="@workspace",
                       background=False, settle="now")
    assert out["state"] == "done"                       # bytes landed
    assert out["unstable"] == ["grow.txt"]              # tear is loud
    dest = Path(out["location"]["path"])
    assert (dest / "grow.txt").read_text() == "seed"    # point-in-time
    sidecar = json.loads((dest / ".weft-run.json").read_text())
    assert sidecar["files"][0]["changed_during_capture"] is True
    ev = _events(w, "retain.unstable")
    assert ev and ev[-1]["paths"] == ["grow.txt"]
    w.kernel_stop(k)


def test_mark_mode_snapshot_marks_now(tmp_path, pixi_bin):
    """durable=true site: no durability gap, but the record, sidecar
    and digests should land while the kernel lives all the same."""
    w = Weft(tmp_path / "ws-m", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local", {"root": str(tmp_path / "site-m"),
                                       "pixi_source": pixi_bin,
                                       "durable": True})
    w.runner.poll_interval = 0.2
    k = w.kernel_start("local", "python")["kernel_id"]
    assert w.kernel_exec(k, "open('table.csv', 'w').write('a,b\\n1,2')",
                         timeout=60)["rc"] == 0
    out = w.run_retain(k, include=["table.csv"], background=False,
                       settle="now")
    assert out["state"] == "done" and out["settle"] == "now"
    assert out["moved"] is False and out["in_place"] is True
    jd = Path(out["location"]["path"])
    sidecar = json.loads((jd / ".weft-run.json").read_text())
    assert sidecar["settle"] == "now"
    assert sidecar["files"][0]["sha256"] == \
        hashlib.sha256(b"a,b\n1,2").hexdigest()
    assert w.kernel_exec(k, "print('alive')", timeout=60)["rc"] == 0
    w.kernel_stop(k)


def test_snapshot_replaces_pending_pin_with_note(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    assert w.kernel_exec(k, "open('c.txt', 'w').write('now-bytes')",
                         timeout=60)["rc"] == 0
    pin = w.run_retain(k, include=["c.txt"], dest="@workspace",
                       background=False)
    assert pin["state"] == "pinned-pending"
    out = w.run_retain(k, include=["c.txt"], dest="@workspace",
                       background=False, settle="now")
    assert out["state"] == "done" and out["pin_replaced"] is True
    assert "settlement will not run" in out["note"]
    # the file changes; stop must NOT resettle over the snapshot
    assert w.kernel_exec(k, "open('c.txt', 'w').write('later-bytes')",
                         timeout=60)["rc"] == 0
    w.kernel_stop(k)
    assert w.store.get_retained(k)["state"] == "done"
    dest = Path(out["location"]["path"])
    assert (dest / "c.txt").read_text() == "now-bytes"  # banked, kept


def test_multi_file_snapshot_digests_batch(w):
    """The digest pass is ONE hashing process per chunk, matched back
    by path — spaces in names must survive the batch parse and every
    file must get its own digest."""
    k = w.kernel_start("local", "python")["kernel_id"]
    assert w.kernel_exec(
        k, "open('a.txt', 'w').write('alpha')\n"
           "open('b name.txt', 'w').write('beta-content')\n"
           "open('c.txt', 'w').write('gamma')", timeout=60)["rc"] == 0
    out = w.run_retain(k, include=["a.txt", "b name.txt", "c.txt"],
                       dest="@workspace", background=False, settle="now")
    assert out["state"] == "done" and "digests" not in out
    sidecar = json.loads(
        (Path(out["location"]["path"]) / ".weft-run.json").read_text())
    by = {f["path"]: f["sha256"] for f in sidecar["files"]}
    assert by == {
        "a.txt": hashlib.sha256(b"alpha").hexdigest(),
        "b name.txt": hashlib.sha256(b"beta-content").hexdigest(),
        "c.txt": hashlib.sha256(b"gamma").hexdigest()}
    w.kernel_stop(k)


def test_digest_tooling_failure_is_soft_and_honest(w):
    """Broken site sha256 tooling: the capture stands, the entry gets
    NO digest (never a fallback identity), the caller sees
    digests: unavailable."""
    class FakeResult:
        rc, out, err = 0, "not-a-digest\n", ""

    class FakeAdapter:
        def run_cmd(self, *a, **kw):
            return FakeResult()

    sel = [{"path": "x.bin", "bytes": 3, "mtime": 1}]
    ok = w.retains._snapshot_digests(FakeAdapter(), sel, "/keep", True,
                                     True)
    assert ok is False and "sha256" not in sel[0]
