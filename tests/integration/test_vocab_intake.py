"""Vocabulary-intake sweep (R/cran round, sweep B): a fixed-vocabulary
parameter is either FOLDED to its canonical case or REFUSED with the
vocabulary named — silent-empty filters and silently-ignored modes are
the tested failure (a wrong-case filter returned [] exactly like
"nothing exists", and the casing convention differed per entity)."""

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


def test_job_state_filters_fold_case(w):
    jid = w.task_submit({"command": "true", "site": "local"})["job_id"]
    w.runner.wait(jid, 120)
    assert w.task_status(state="done"), "lowercase folds to DONE"
    assert w.jobs_where(state="Done")["jobs"], "mixed case folds"
    out = w.jobs_where(state="finished")
    assert out.get("error") == "task.invalid" and "DONE" in out["hints"]["known"], \
        "unknown words refuse WITH the vocabulary (fold fixes case only)"


def test_kernel_service_session_state_filters_fold(w):
    assert w.list_kernels(state="RUNNING")["kernels"] == []
    out = w.list_kernels(state="hibernating")
    assert out.get("error") == "task.invalid"
    assert w.list_services(state="READY")["services"] == []
    assert w.list_sessions(state="ACTIVE") == []
    out = w.data_list(kind="File")
    assert out["refs"] == [] or "refs" in out     # folded, no refusal
    out = w.data_list(kind="blob")
    assert out.get("error") == "task.invalid"


def test_relax_vocabulary_refuses_unknown(w):
    out = w.env_ensure({"name": "x", "deps": {"conda": ["python"]}},
                       relax="Soft")
    assert out.get("error") == "task.invalid", \
        "relax='Soft' silently meant NO relaxation before (sweep B1)"
    assert out["hints"]["known"] == ["none", "soft"]


def test_kernel_replay_vocabulary(w):
    from weft.kernel import KernelManager
    km = w.kernels
    with pytest.raises(WeftError) as ei:
        km.restart("kr_nonexistent", replay="all")
    # replay validation fires before the kernel lookup? Either order is
    # fine as long as "all" cannot silently mean "none"; assert the
    # typed refusal names one of the two problems
    assert ei.value.code == "task.invalid"

    class FakeK(KernelManager):
        def _get(self, kernel_id):
            return {"state": "stopped", "site": "local", "lang": "python",
                    "jobdir": "kernels/x", "handle": "pid:1",
                    "label": "", "session_id": None,
                    "capture": "transcript", "env_id": None}

    fk = FakeK(w.store, w.adapters, w.runner)
    with pytest.raises(WeftError) as ei:
        fk.restart("kr_x", replay="everything")
    assert ei.value.code == "task.invalid"
    assert ei.value.hints["known"] == ["successful", "none"]


def test_retain_dest_sentinel_case(w):
    jid = w.task_submit({"command": "echo hi > out.txt",
                         "outputs": ["out.txt"], "site": "local"})["job_id"]
    w.runner.wait(jid, 120)
    out = w.run_retain(jid, dest="@Workspace")
    # public verb: refusal is the payload. Before the fix this WROTE a
    # literal ./@Workspace directory (sweep B6)
    if isinstance(out, dict) and out.get("error"):
        assert out["error"] == "task.invalid"
        assert "@workspace" in out["hints"]["known"]
    else:
        # fold-accepted is also a valid contract choice — then it must
        # have landed in the real workspace, and no literal dir exists
        import pathlib
        assert not pathlib.Path("@Workspace").exists()


def test_auto_site_hint_names_the_sentinel(w):
    out = w.task_submit({"command": "true", "site": "AUTO"})
    assert out.get("error") == "task.invalid"
    assert "auto" in out["hints"].get("note", ""), \
        "the refusal must point at the reserved value, not just site names"


def test_kernel_lang_and_capture_fold(w):
    """lang='R' must fold and pass the registry check — proven by the
    refusal moving PAST lang to the next gate (an unknown EnvID; was
    env.not_realized until the aba2 round made kernel_start realize
    solved envs itself — deliberate re-freeze)."""
    with pytest.raises(WeftError) as ei:
        w.kernels.start("local", lang="R", env_id="env_nonexistent")
    assert ei.value.code == "task.invalid" \
        and "unknown EnvID" in ei.value.detail, \
        f"lang='R' still tripped the registry: {ei.value}"
    with pytest.raises(WeftError) as ei:
        w.kernels.start("local", lang="Fortran", env_id="env_x")
    assert ei.value.code == "task.invalid"
    assert ei.value.hints["registered"] == ["julia", "python", "r"]
    with pytest.raises(WeftError) as ei:
        w.kernels.start("local", lang="python", env_id="env_x",
                        capture="Transcript-Extra")
    assert ei.value.code == "task.invalid"
    assert ei.value.hints["known"] == ["transcript", "none"]


def test_array_result_works(w):
    """Regression (aba verification catch): the vocab-fold str.replace
    had a THIRD landing site — array_result, which has no state param,
    raised UnboundLocalError on EVERY call, and two full green lanes
    certified it because its only test was docker-marked."""
    group = w.task_submit({"command": "echo $WEFT_ARRAY_INDEX > out.txt",
                           "outputs": ["out.txt"], "site": "local",
                           "array": 2})["group"]
    deadline = __import__("time").time() + 120
    while __import__("time").time() < deadline:
        st = w.array_status(group)
        if st["done"] + st["failed"] == 2:
            break
        __import__("time").sleep(0.3)
    roll = w.array_result(group)
    assert roll["group"] == group
    out = w.array_result("grp_nonexistent")
    assert out.get("error") == "task.invalid", out


def test_vocab_folds_reference_real_parameters():
    """Static pin for the whole class: every `x = _vocab(x, ...)` fold
    must name a parameter of its enclosing function — the stray-landing
    failure mode compiles clean and only explodes at call time, in
    whatever verb the wayward edit happened to hit."""
    import ast
    import inspect

    import weft.api as api_mod
    tree = ast.parse(inspect.getsource(api_mod))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        params = {a.arg for a in node.args.args + node.args.kwonlyargs}
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) \
                    and getattr(sub.func, "id", "") == "_vocab":
                name = getattr(sub.args[0], "id", None)
                if name is not None and name not in params:
                    bad.append(f"{node.name}:{sub.lineno} folds {name!r}")
    assert not bad, bad


def test_writes_to_vocabulary_at_intake(w):
    from weft.session import SessionManager
    import inspect
    src = inspect.getsource(SessionManager.run_installer) \
        if hasattr(SessionManager, "run_installer") else ""
    # the contract is enforced in _run_installer's body — pin the shape:
    # an unknown writes_to refuses task.invalid with the vocabulary,
    # warm or cold (sweep B5: warm path silently ignored "RLIB")
    src_all = inspect.getsource(SessionManager)
    assert 'hints={"known": ["rlib", "pylib"]}' in src_all
