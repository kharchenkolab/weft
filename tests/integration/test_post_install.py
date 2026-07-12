"""post_install: the escape hatch actually executes, inside the activated
env, with structured failure — and failures name the command."""

import pytest

from weft.api import Weft

pytestmark = pytest.mark.solver


@pytest.fixture
def weft(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def test_post_install_runs_in_activated_env(weft):
    env = weft.env_ensure({
        "name": "with-hatch",
        "deps": {"conda": ["xz >=5"]},
        # emulates a bespoke installer: sees the env's tools, writes into it
        "post_install": [
            "xz --version > post-marker.txt",
            "mkdir -p extra/bin && printf '#!/bin/sh\\necho custom-tool-ok\\n'"
            " > extra/bin/mytool && chmod +x extra/bin/mytool",
        ],
    })
    assert "env_id" in env, env
    assert weft.env_status(env["env_id"])["summary"]["weakly_reproducible"]
    r = weft.task_submit({"command": "true", "env": env["env_id"],
                          "site": "local"})
    job = weft.runner.wait(r["job_id"], 300)   # realization happens here
    assert job["state"] == "DONE", job["error"]
    from weft.realize import env_dir_rel
    rel = env_dir_rel(env["env_id"])
    site = weft.adapters["local"]
    assert site.file_exists(f"{rel}/post-marker.txt")
    out = site.run_cmd(f"$WEFT_ROOT/{rel}/extra/bin/mytool")
    assert out.out.strip() == "custom-tool-ok"


def test_post_install_failure_is_structured(weft):
    res = weft.env_ensure({
        "name": "broken-hatch",
        "deps": {"conda": ["xz >=5"]},
        "post_install": ["definitely-not-a-command --flag"],
    })
    env_id = res["env_id"]
    r = weft.task_submit({"command": "true", "env": env_id, "site": "local"})
    job = weft.runner.wait(r["job_id"], 300)
    assert job["state"] == "FAILED"
    err = job["error"]
    assert err["error"] == "env.realize_failed"
    assert "definitely-not-a-command" in err["hints"]["command"]
    assert "air-gapped" in err["hints"]["note"]
