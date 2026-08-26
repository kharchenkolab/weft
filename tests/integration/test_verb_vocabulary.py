"""One vocabulary on the verb surface (census: the same concept
spelled differently across sibling verbs cost live round-trips —
target={"env"} vs env_id=, at= vs site=, why= vs reason=, and
session_start(env_id=) accepting an inline spec while kernel_start's
SAME-NAMED parameter refused one). Aliases accept the sibling
spelling; both-set-and-different refuses; the canonical spelling
stays canonical."""

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    return Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")


def test_kernel_start_accepts_an_inline_spec(w, tmp_path, monkeypatch):
    """session_start(env_id=) takes an EnvID or an inline spec;
    kernel_start's parameter has the SAME NAME and refused the spec —
    an agent that learned the session shape was wrong on the sibling.
    The spec now auto-ensures (enable, don't refuse)."""
    seen = {}

    def spy(*args, **kw):
        seen["env_id"] = kw.get("env_id") or next(
            (a for a in args if isinstance(a, str)
             and a.startswith("env:")), None)
        return {"kernel_id": "kr_test"}
    monkeypatch.setattr(w.kernels, "start", spy)
    monkeypatch.setattr(
        w, "env_ensure",
        lambda spec: {"env_id": "env:v1:" + "cd" * 32,
                      "status": "solved"})
    got = w.kernel_start("local", env_id={"name": "inline",
                                          "deps": {"conda": []}})
    assert "error" not in got, got
    assert seen["env_id"] == "env:v1:" + "cd" * 32


def test_kernel_start_inline_spec_solve_failure_propagates(w,
                                                           monkeypatch):
    monkeypatch.setattr(
        w, "env_ensure",
        lambda spec: {"error": "env.solve_conflict", "detail": "no",
                      "hints": {}})
    got = w.kernel_start("local", env_id={"name": "bad",
                                          "deps": {"conda": ["x"]}})
    assert got["error"] == "env.solve_conflict"


def test_data_evict_accepts_site_alias(w):
    """data_evict spelled the place at= while 24 sibling verbs say
    site= — the alias must reach the SAME lane (and then fail on the
    unknown ref, not on the argument)."""
    a = w.data_evict("dref:sha256:" + "0" * 64, at="@workspace",
                     dry_run=True)
    b = w.data_evict("dref:sha256:" + "0" * 64, site="@workspace",
                     dry_run=True)
    assert a.get("error") == b.get("error")
    assert b.get("error") != "tool.bad_arguments"


def test_data_evict_conflicting_alias_refuses(w):
    got = w.data_evict("dref:sha256:" + "0" * 64, at="@workspace",
                       site="elsewhere")
    assert got["error"] == "task.invalid"
    assert "at" in got["detail"] and "site" in got["detail"]


def test_data_list_accepts_site_alias(w):
    got = w.data_list(site="nowhere-site")
    assert got.get("error") != "tool.bad_arguments"
    assert got["refs"] == []


def test_why_and_reason_cross_aliases(w):
    """why= (task_cancel, site_exec, job_node_exec) vs reason=
    (env_revise): each accepts the sibling's word."""
    got = w.task_cancel("jb_nonexistent", reason="stuck")
    assert got.get("error") != "tool.bad_arguments"
    got2 = w.env_revise("env:v1:" + "ab" * 32, why="index moved")
    assert got2.get("error") != "tool.bad_arguments"


def test_dict_params_carry_schema_hints():
    """The MCP schema is the agent's only signal for dict shapes
    (census: 3 of ~90 verbs had hints; the missing ensure_available
    entry was item 2b's direct mechanism). Every dict-annotated
    public-verb parameter needs a SCHEMA_HINTS entry or a place on
    the allowlist below — which may only SHRINK."""
    import inspect

    from weft.api import PUBLIC_TOOLS, Weft as W
    from weft.mcp_server import SCHEMA_HINTS

    ALLOWED_WITHOUT_HINTS = {
        # filters/options whose keys the docstring first paragraph
        # already enumerates, or pass-through payloads
        ("audit_tail", "filters"), ("events_poll", "filters"),
        ("bundle_export", "metadata"), ("run_retain", "policy"),
        ("site_load", "resources"), ("kernel_start", "resources"),
        ("data_register", "meta"), ("site_note", "note"),
        ("gc_plan", "policy"), ("gc_sweep", "policy"),
        ("reconcile", "opts"),
    }
    missing = []
    for v in PUBLIC_TOOLS:
        fn = getattr(W, v)
        fn = getattr(fn, "_weft_unwrapped", fn)
        for pname, p in inspect.signature(fn).parameters.items():
            ann = str(p.annotation)
            if "dict" in ann and pname != "self":
                if SCHEMA_HINTS.get(v, {}).get(pname):
                    continue
                if (v, pname) in ALLOWED_WITHOUT_HINTS:
                    continue
                missing.append((v, pname))
    assert not missing, (
        f"dict params with no MCP schema hint: {missing} — add a "
        f"SCHEMA_HINTS entry (the shape belongs in the schema, not "
        f"only the docstring)")


def test_ensure_available_accepts_a_kernel_target(w, monkeypatch):
    """aba2 ask: the kernel is where the agent IS — a kernel target
    resolves to the kernel's session (or env) instead of refusing."""
    monkeypatch.setattr(w.store, "get_kernel",
                        lambda kid: {"kernel_id": kid,
                                     "session_id": "sess_abc",
                                     "env_id": None})
    seen = {}

    def spy(sid, adapter, request, **kw):
        seen["sid"] = sid
        return {"satisfied": True}
    monkeypatch.setattr(w.sessions, "ensure_available", spy)
    monkeypatch.setattr(w, "_session_adapter", lambda sid: "adapter")
    got = w.ensure_available({"kernel": "kr_1"}, {"pypi": ["toolpkg"]})
    assert "error" not in got, got
    assert seen["sid"] == "sess_abc"


def test_ensure_available_unknown_kernel_refuses_typed(w):
    got = w.ensure_available({"kernel_id": "kr_nope"}, {"pypi": ["x"]})
    assert got["error"] == "task.invalid"
    assert "kernel" in got["detail"]


def test_run_input_on_nonterminal_run_names_the_real_cause(w,
                                                           monkeypatch):
    """aba2 slurm-array journey: a dependent submit referencing a
    QUEUED run got 'the sandbox may be swept' + existed:True — the
    hint must say the run has not landed."""
    monkeypatch.setattr(
        w.store, "get_job",
        lambda jid: {"job_id": jid, "state": "QUEUED", "site": "local",
                     "sched_handle": None, "task": {}})
    monkeypatch.setattr(w.retains, "_stat_one", lambda c: None)
    monkeypatch.setattr(w.retains, "_sandbox_path",
                        lambda t, r: (None, None, None))
    got = w.data_register(run="jb_pending", rel="results/out.txt")
    assert got["error"] == "data.missing"
    assert "QUEUED" in got["detail"]
    assert "AFTER the run lands" in got["hints"]["suggestion"]
    assert "swept" not in str(got["hints"])
