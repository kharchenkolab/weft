"""Footprint round 26 companions to data_evict: as_actor audit
attribution (embedder-scoped, deliberately NOT a tool parameter), the
typed `external` flag on describe locations, and run_forget's
record_only receipt for keep-anchored refs whose only bytes were the
forgotten keep."""

import threading

import pytest

from weft.api import PUBLIC_TOOLS, Weft
from weft.errors import WeftError


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def _run(w, cmd, outputs=None):
    r = w.task_submit({"command": cmd, "site": "local",
                       **({"outputs": outputs} if outputs else {})})
    job = w.runner.wait(r["job_id"], 120)
    assert job["state"] == "DONE", job.get("error")
    return r["job_id"], job


# -- as_actor ------------------------------------------------------------

def test_as_actor_stamps_rows_inside_context_only(w):
    w.site_note("local", "before")
    with w.as_actor("agent:c-123"):
        w.site_note("local", "inside")
    w.site_note("local", "after")
    actors = [r["actor"] for r in w.store.audit_tail(50)
              if r["action"] == "site.note"]
    assert actors == ["agent", "agent:c-123", "agent"]


def test_as_actor_nesting_restores_outer(w):
    with w.as_actor("agent:outer"):
        with w.as_actor("agent:inner"):
            w.site_note("local", "in")
        w.site_note("local", "out")
    actors = [r["actor"] for r in w.store.audit_tail(50)
              if r["action"] == "site.note"]
    assert actors == ["agent:inner", "agent:outer"]


def test_as_actor_hygiene_refusals(w):
    for bad in ("", "   ", "a\x00b", "line\nbreak", "x" * 201, 42):
        with pytest.raises(WeftError) as e:
            with w.as_actor(bad):
                pass
        assert e.value.code == "task.invalid", bad


def test_as_actor_does_not_leak_across_threads(w):
    """Contextvar semantics pinned: a worker thread (runner, retain
    queue) writing audit rows while the facade thread sits inside
    as_actor still stamps the constructor default — attribution covers
    the calls the actor MADE, not whatever the machinery does
    concurrently."""
    with w.as_actor("agent:owner"):
        t = threading.Thread(
            target=lambda: w.store.audit_log(None, "thread.probe"))
        t.start()
        t.join()
    row = [r for r in w.store.audit_tail(50)
           if r["action"] == "thread.probe"][-1]
    assert row["actor"] == "agent"


def test_as_actor_is_not_a_public_tool():
    """The recorded design refusal: a per-call actor on the tool
    surface would let an agent write someone else's name into the
    trail. Embedders reach it on the object; agents cannot."""
    assert "as_actor" not in PUBLIC_TOOLS
    assert "data_evict" in PUBLIC_TOOLS          # the verb IS public


# -- typed external flag ---------------------------------------------------

def test_describe_locations_external_is_typed(w, tmp_path):
    d = tmp_path / "site" / "perm" / "held"
    d.mkdir(parents=True)
    (d / "f.bin").write_bytes(b"y" * 8)
    ext = w.data_register(str(d), site="local", ingest=False)["ref"]
    rows = w.data_describe(ext)["locations"]
    assert {r["site"]: r["external"] for r in rows}["local"] is True

    p = tmp_path / "owned.bin"
    p.write_bytes(b"owned")
    ref = w.data_register(str(p))["ref"]
    jid = w.task_submit({"command": "wc -c < data/in.bin > n.txt",
                         "inputs": [{"ref": ref,
                                     "mount_as": "data/in.bin"}],
                         "outputs": ["n.txt"],
                         "site": "local"})["job_id"]
    assert w.runner.wait(jid, 120)["state"] == "DONE"
    rows = w.data_describe(ref)["locations"]
    assert rows and all(r["external"] is False for r in rows)


# -- run_forget record_only -----------------------------------------------

def test_forget_reports_record_only_strandeds(w, tmp_path):
    """The pre-flight warning and the outcome agree: forgetting a keep
    that held a ref's ONLY bytes names that ref in the receipt —
    identity and provenance survive, bytes do not."""
    jid, job = _run(w, "printf preciousss > results/out.bin",
                    ["results/"])
    ref = next(o["ref"] for o in job["manifest"]["outputs"]
               if o["path"] == "results/out.bin")
    w.run_retain(jid, include=["results/out.bin"], dest="@workspace",
                 background=False)
    assert w.store.get_dataref(ref)["meta"]["keep"]["target"] == jid
    w.run_discard(jid)                       # sandbox gone
    got = w.data_evict(ref, at="local")      # site copy gone; keep holds
    assert "error" not in got, got
    out = w.run_forget(target=jid)
    entry = next(f for f in out["forgotten"] if f["target"] == jid)
    assert entry["record_only"] == [ref]
    row = w.store.get_dataref(ref)
    assert not (row["meta"] or {}).get("keep")     # anchor stripped
    assert row["ref"] == ref                       # the record survives


def test_forget_omits_record_only_when_bytes_survive(w, tmp_path):
    """Same forget, but the site CAS copy still exists — the ref is
    re-obtainable, so the receipt stays quiet about it."""
    jid, job = _run(w, "printf durable > results/out.bin", ["results/"])
    ref = next(o["ref"] for o in job["manifest"]["outputs"]
               if o["path"] == "results/out.bin")
    w.run_retain(jid, include=["results/out.bin"], dest="@workspace",
                 background=False)
    w.run_discard(jid)
    out = w.run_forget(target=jid)
    entry = next(f for f in out["forgotten"] if f["target"] == jid)
    assert "record_only" not in entry
    assert not (w.store.get_dataref(ref)["meta"] or {}).get("keep")
    assert any(l["site"] == "local"
               for l in w.store.locations_of(ref))
