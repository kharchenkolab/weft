"""Path inputs declare a REALM (controller|site) and land where they
aim. The landing-site census (tilde audit, 2026-08-25): '~' in a site
root survived registration, probe, and shim — shlex.quote suppresses
tilde expansion in every ssh command, pathlib never expands — and
finally detonated 185s later as a rattler NotAbsolute PANIC (4th live
hit). pixi_source with '~' crashed ssh registration raw and SILENTLY
SKIPPED the tool push locally; data_register('~/f') landed at
<workspace>/~/f. These tests drive the shipped shapes; HOME is
monkeypatched so 'the site user's home' is a tmp dir."""

import json
from pathlib import Path

import pytest

from weft.api import Weft


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "fakehome"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


def _w(tmp_path, pixi_bin):
    return Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")


def test_register_site_resolves_tilde_root_site_side(tmp_path, pixi_bin,
                                                     home):
    """The incident shape: root '~/aba2-work'. Registration resolves it
    against the SITE's home (for a local site that is $HOME), stores
    the ABSOLUTE path, echoes the resolution, and the site actually
    works — no literal '~' directory anywhere."""
    w = _w(tmp_path, pixi_bin)
    got = w.register_site("local", "local",
                          {"root": "~/weft-root",
                           "pixi_source": pixi_bin}, tools="skip")
    assert "error" not in got, got
    assert got["resolved_paths"]["root"] == str(home / "weft-root")
    row = w.store.get_site("local")
    assert row["config"]["root"] == str(home / "weft-root")
    assert (home / "weft-root").is_dir()          # bootstrap landed home
    assert not (tmp_path / "ws" / "~").exists()   # no literal ~ anywhere
    # (no cwd-global '~' assert: the RED-PROOF run itself litters the
    # cwd on pre-fix code — the exact bug — and a later green run must
    # not inherit that state)


def test_register_site_resolves_policy_storage_tilde(tmp_path, pixi_bin,
                                                     home):
    """policy.storage values are exported into EVERY job's cmd.sh —
    a literal '~' there reaches the user's own command unexpanded
    (census: silent misbehavior propagated into user jobs)."""
    w = _w(tmp_path, pixi_bin)
    got = w.register_site(
        "local", "local",
        {"root": str(tmp_path / "site"), "pixi_source": pixi_bin,
         "policy": {"storage": {"scratch": "~/scr"}}}, tools="skip")
    assert "error" not in got, got
    row = w.store.get_site("local")
    assert row["config"]["policy"]["storage"]["scratch"] == \
        str(home / "scr")


def test_restored_tilde_row_refuses_typed_never_panics(tmp_path,
                                                       pixi_bin):
    """Pre-fix rows are already in stores (config persists verbatim;
    the registration gate never re-runs on restore): building an
    adapter from one must REFUSE typed, naming re-register — the
    alternative is the 185s-later rust panic."""
    w = _w(tmp_path, pixi_bin)
    w.store.put_site("stale", "local", {"root": "~/stale-root"})
    got = w.site_exec("stale", "true", "probe the stale row")
    assert got["error"] == "task.invalid", got
    assert "re-register" in (got["detail"] + str(got["hints"]))
    assert "~" in got["detail"] or "~" in str(got["hints"])


def test_pixi_source_is_controller_realm_and_must_exist(tmp_path,
                                                        pixi_bin, home):
    """pixi_source names a CONTROLLER file: '~' resolves against the
    controller's home, and a missing source REFUSES at registration —
    the local lane used to silently skip the push and the site surfaced
    tool-less, unattributably, at first realize."""
    w = _w(tmp_path, pixi_bin)
    got = w.register_site("local", "local",
                          {"root": str(tmp_path / "site2"),
                           "pixi_source": "~/no-such-pixi"},
                          tools="skip")
    assert got["error"] == "task.invalid", got
    assert "pixi_source" in got["detail"]
    assert str(home / "no-such-pixi") in got["detail"] + str(got["hints"])


def test_data_register_tilde_lands_in_home_not_workspace(tmp_path,
                                                         pixi_bin,
                                                         home):
    """'~/f' was treated as workspace-RELATIVE (Path('~/f') is not
    absolute) and landed at <workspace>/~/f — silent misregistration
    of the wrong file."""
    w = _w(tmp_path, pixi_bin)
    (home / "data.bin").write_bytes(b"payload-bytes")
    got = w.data_register("~/data.bin")
    assert "error" not in got, got
    desc = w.data_describe(got["ref"])
    blob = json.dumps(desc)
    assert "/~/" not in blob
    assert not (tmp_path / "ws" / "~").exists()


def test_durable_tilde_resolves_then_validates(tmp_path, pixi_bin,
                                               home):
    """durable already had the one correct refusal (startswith '/');
    with home resolution it now ACCEPTS '~/keep' by resolving first —
    enable-and-inform over refuse-and-retry."""
    w = _w(tmp_path, pixi_bin)
    got = w.register_site("local", "local",
                          {"root": str(tmp_path / "site3"),
                           "pixi_source": pixi_bin,
                           "durable": "~/keep"}, tools="skip")
    assert "error" not in got, got
    row = w.store.get_site("local")
    assert row["config"]["durable"] == str(home / "keep")
