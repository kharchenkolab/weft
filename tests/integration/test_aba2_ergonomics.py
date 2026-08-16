"""aba2 ergonomics round: kernel_start realizes on weft's errand (the
attach-verb philosophy is now uniform), promoted jobs carry a terminal
inventory receipt, and promote takes a label."""

import pytest

from weft.api import Weft
from weft.errors import WeftError


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


# -- item 1: kernel_start auto-realize ---------------------------------------

def test_kernel_start_unknown_env_still_refuses(w):
    with pytest.raises(WeftError) as ei:
        w.kernels.start("local", lang="python",
                        env_id="env:v1:" + "0" * 64)
    assert ei.value.code == "task.invalid"
    assert "unknown EnvID" in ei.value.detail


def test_kernel_start_auto_realizes_solved_env(w, monkeypatch):
    """The refusal-and-recovery ceremony (env.not_realized ->
    env_realize -> retry, executed verbatim twice per aba2 thread) is
    gone: kernel_start realizes the solved env itself, through the SAME
    ensure_realization door session_start uses."""
    from weft.realize import env_dir_rel
    env_id = "env:v1:" + "e" * 64
    w.store.put_env(env_id, "spec:x", {"platforms": {}}, "", "", ["any"])
    calls = []

    def fake_realize(eid, env_row, adapter, store, caps=None,
                     site_config=None, pack_tools=None):
        calls.append(eid)
        rel = env_dir_rel(eid)
        adapter.write_file(f"{rel}/activate.sh", b": ok\n")
        adapter.write_file(f"{rel}/.weft-ready", b"{}\n")
        store.set_realization(eid, adapter.name, "prefix", rel, "ready")

    import weft.realize as realize_mod
    monkeypatch.setattr(realize_mod, "ensure_realization", fake_realize)
    r = w.kernel_start("local", lang="python", env_id=env_id)
    assert "kernel_id" in r, r
    assert calls == [env_id], "auto-realize must run exactly once"
    out = w.kernel_exec(r["kernel_id"], "print(6*7)")
    assert out["rc"] == 0 and "42" in out["out"]
    calls.clear()
    r2 = w.kernel_start("local", lang="python", env_id=env_id)
    assert "kernel_id" in r2
    assert calls == [], "already realized -> no realize call"
    w.kernel_stop(r["kernel_id"])
    w.kernel_stop(r2["kernel_id"])


@pytest.mark.solver
def test_kernel_start_auto_realizes_for_real(w):
    """The aba2 trace end-to-end with a REAL env: ensure (solved) ->
    kernel_start with NO env_realize in between -> block runs."""
    from weft.spec import current_platform
    env_id = w.env_ensure({"name": "kmini",
                           "platforms": [current_platform()],
                           "deps": {"conda": ["python =3.12"]}})["env_id"]
    r = w.kernel_start("local", lang="python", env_id=env_id)
    assert "kernel_id" in r, r
    out = w.kernel_exec(r["kernel_id"], "import sys; print(sys.version[:4])")
    assert out["rc"] == 0 and "3.12" in out["out"]
    events = [e["kind"] for e in w.store.events_since(0, 2000)]
    assert "realize.prefix" in events, \
        "the auto-realize narrates like any realize"
    w.kernel_stop(r["kernel_id"])


# -- items 2+3: promote receipt + label ---------------------------------------

def _promoted(w, label=None, kernel_label=""):
    k = w.kernel_start("local", "python", label=kernel_label)["kernel_id"]
    r = w.kernel_exec(
        k, "import os\n"
           "open(os.environ['WEFT_BLOCK_DIR'] + '/hist.txt', 'w')"
           ".write('bins')")
    assert r["rc"] == 0
    kwargs = {} if label is None else {"label": label}
    m = w.kernel_promote(k, blocks=[r["block"]], **kwargs)
    w.kernel_stop(k)
    return k, m


def test_promoted_job_has_inventory_receipt(w):
    """'Terminal state implies a receipt exists' now holds for promoted
    jobs — both run_inventory doors refused before (no receipt, no
    jobs/<jid> sandbox), and the agent fell back to data_register on
    kernel-scratch paths, losing the job linkage (aba2)."""
    _, m = _promoted(w)
    inv = w.run_inventory(m["job_id"])
    assert not inv.get("error"), inv
    entries = {e["path"]: e for e in inv["entries"]}
    art = next(p for p in entries if p.endswith("hist.txt"))
    assert entries[art]["bytes"] == 4
    assert "mtime" not in entries[art], \
        "we did not stat — honest absence over a fabricated timestamp"
    assert not any(e.get("scaffold") for e in inv["entries"])
    # the invariant, checked across every terminal job this workspace has
    for j in w.jobs_where(state="DONE")["jobs"]:
        if j["manifest"]:
            r = w.run_inventory(j["job_id"])
            assert not r.get("error"), (j["job_id"], r)


def test_promote_label_inherits_and_overrides(w):
    _, m = _promoted(w, kernel_label="phase catalog")
    row = w.task_status(m["job_id"])[0]
    assert row["label"] == "phase catalog", "default inherits the kernel's"
    _, m2 = _promoted(w, label="phase 9 rollup")
    assert w.task_status(m2["job_id"])[0]["label"] == "phase 9 rollup"


def test_promote_label_is_not_identity(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    r = w.kernel_exec(
        k, "import os\n"
           "open(os.environ['WEFT_BLOCK_DIR'] + '/x.txt', 'w').write('v')")
    m1 = w.kernel_promote(k, blocks=[r["block"]], label="first name")
    m2 = w.kernel_promote(k, blocks=[r["block"]], label="second name")
    w.kernel_stop(k)
    assert m1["task_hash"] == m2["task_hash"], \
        "labels are display handles — relabeling must never fork the record"
