"""Step 2: exploration should cost one call, and bespoke installs are
normal moves that survive into the record."""

import pytest

from weft.api import Weft

pytestmark = [pytest.mark.solver, pytest.mark.slow]


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    return w


def test_session_from_spec_in_one_call(w):
    """No ensure → throwaway task → start dance: hand it a spec."""
    s = w.session_start({"name": "explore",
                         "deps": {"conda": ["python =3.12"]}}, "local")
    assert "session_id" in s, s
    sid = s["session_id"]
    assert w.session_exec(sid, "python -c 'print(1)'")["rc"] == 0
    w.session_stop(sid)


def test_bespoke_installer_is_captured_and_carried(w):
    s = w.session_start({"name": "hatch", "deps": {"conda": ["python =3.12",
                                                             "pip"]}},
                        "local")
    sid = s["session_id"]
    # the kind of fix that unblocks real work and no index expresses:
    # install a locally-built wheel-less package from source
    w.session_exec(sid, "mkdir -p pkg/mymod && "
                        "printf 'def hi():\\n    return \"vendored\"\\n' "
                        "> pkg/mymod/__init__.py && "
                        "printf '[project]\\nname=\"mymod\"\\n"
                        "version=\"0.1\"\\n' > pkg/pyproject.toml")
    r = w.session_run_installer(sid, "pip install ./pkg",
                                note="vendored: upstream has no release yet")
    assert r["captured"] is True, r
    assert w.session_exec(
        sid, "python -c 'import mymod; print(mymod.hi())'")["rc"] == 0

    snap = w.session_snapshot(sid, name="with-vendored",
                              notes=["kept until upstream 0.2 ships"])
    assert snap["carried_installers"] == 1
    assert snap["spec"]["post_install"] == ["pip install ./pkg"]
    assert "upstream" in snap["spec"]["step_notes"]["0"]

    # the snapshot env is graded honestly, and the rationale is in the record
    st = w.env_status(snap["env_id"])["summary"]
    assert st["reproducibility"] == "escape-hatch"
    assert st["notes"] == ["kept until upstream 0.2 ships"]
    assert st["step_notes"]["0"].startswith("vendored")
    w.session_stop(sid)
