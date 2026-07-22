"""P2 solver-lane: the realize postcondition against a REAL solve and
build — ready means verified, on an actual prefix."""

import pytest

from weft.api import Weft

pytestmark = [pytest.mark.solver, pytest.mark.slow]


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def _realize(w, env_id):
    r = w.task_submit({"command": "true", "env": env_id, "site": "local"},
                      force=True)
    return w.runner.wait(r["job_id"], 2400)


def test_realize_postcondition_passes_on_real_build(w):
    env = w.env_ensure({"name": "verified-env",
                        "deps": {"conda": ["python =3.12", "pip"]},
                        "verify": {"import": ["pip"],
                                   "versions": {"pip": ">=20"}}})
    assert _realize(w, env["env_id"])["state"] == "DONE"


def test_realize_postcondition_failure_blocks_ready_on_real_build(w):
    env = w.env_ensure({"name": "impossible-claim",
                        "deps": {"conda": ["python =3.12", "pip"]},
                        "verify": {"versions": {"pip": "==0.0.1"}}})
    job = _realize(w, env["env_id"])
    assert job["state"] == "FAILED"
    assert job["error"]["error"] == "env.realize_failed"
    assert job["error"]["hints"].get("postcondition") or \
        "postcondition" in job["error"]["detail"]


def test_verify_block_does_not_fork_the_env_id(w):
    a = w.env_ensure({"name": "same", "deps": {"conda": ["xz"]}})
    b = w.env_ensure({"name": "same", "deps": {"conda": ["xz"]},
                      "verify": {"versions": {"xz": ">=5"}}})
    assert a["env_id"] == b["env_id"]     # identity-neutral, END TO END
