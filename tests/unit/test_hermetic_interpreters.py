"""The interpreter-hermeticity umbrella: ONE owner
(runner_util.hermetic_interpreter_lines), consumed by tasks, kernels
and sessions POST-activation. Motivating incidents (aba2 R_LIBS_USER
ask): a user R library with a mismatched libR entered .libPaths and
segfaulted deterministically (read as 'the package crashed'); an
in-env install defaulted to the USER library and broke ~ when the env
was cleaned. PIP_CONFIG_FILE: a user pip.conf can override index-url
— installs resolving against an invisible index."""

from weft.runner_util import hermetic_interpreter_lines


def test_umbrella_covers_the_three_vectors():
    lines = hermetic_interpreter_lines()
    joined = "\n".join(lines)
    assert "PYTHONNOUSERSITE=1" in joined
    assert 'R_LIBS_USER="${WEFT_PREFIX:-$PWD}/lib/R/' in joined
    assert "PIP_CONFIG_FILE=/dev/null" in joined


def test_task_cmd_sh_carries_the_umbrella(tmp_path, pixi_bin):
    """Behavioral, through the real composer: every job's cmd.sh
    exports all three vectors (task env_vars can still override)."""
    from weft.api import Weft
    from weft.task import Task
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local",
                    {"root": str(tmp_path / "site"),
                     "pixi_source": pixi_bin}, tools="skip")
    task = Task.from_dict({"command": "true", "site": "local"})
    lines = w.runner._cmd_lines(task, {}, "local", "jb_x")
    joined = "\n".join(lines)
    assert "PYTHONNOUSERSITE=1" in joined
    assert "R_LIBS_USER=" in joined
    assert "PIP_CONFIG_FILE=/dev/null" in joined
    w.close()


def test_session_composition_carries_the_umbrella(tmp_path, pixi_bin,
                                                  monkeypatch):
    from weft.api import Weft
    w = Weft(tmp_path / "ws2", pixi_bin=pixi_bin, resume="off")
    monkeypatch.setattr(w.sessions, "_stack_activation",
                        lambda s, a: ("ACTIVATE", False))
    class _A:
        name = "local"
        def path(self, rel):
            return f"/root/{rel}"
    pre, ns = w.sessions._composed({"location": "sessions/s1"}, _A())
    assert pre.index("ACTIVATE") < pre.index("R_LIBS_USER")  # post-act
    assert "PIP_CONFIG_FILE=/dev/null" in pre
    w.close()
