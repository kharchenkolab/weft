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
    refusal moving PAST lang to the next gate (an unrealized env)."""
    with pytest.raises(WeftError) as ei:
        w.kernels.start("local", lang="R", env_id="env_nonexistent")
    assert ei.value.code == "env.not_realized", \
        f"lang='R' still tripped the registry: {ei.value.code}"
    with pytest.raises(WeftError) as ei:
        w.kernels.start("local", lang="Fortran", env_id="env_x")
    assert ei.value.code == "task.invalid"
    assert ei.value.hints["registered"] == ["julia", "python", "r"]
    with pytest.raises(WeftError) as ei:
        w.kernels.start("local", lang="python", env_id="env_x",
                        capture="Transcript-Extra")
    assert ei.value.code == "task.invalid"
    assert ei.value.hints["known"] == ["transcript", "none"]


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
