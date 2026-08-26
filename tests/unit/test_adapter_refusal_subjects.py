"""Subject sweep round 2 (adapters): refusals name their SITE — the
distinct-inputs-distinguishable property for the transport layer
(sibling raises already carried self.name; these seven had dropped
it)."""

import pytest

from weft.adapters.local import LocalAdapter
from weft.errors import WeftError


def test_local_missing_file_names_the_site(tmp_path):
    a = LocalAdapter("scratchbox", str(tmp_path / "root"))
    with pytest.raises(WeftError) as ei:
        a.read_file("no/such/file")
    assert ei.value.code == "data.missing"
    assert "scratchbox" in ei.value.detail
    assert ei.value.hints["site"] == "scratchbox"


def test_argument_echoes_name_their_subjects(tmp_path, pixi_bin):
    """Subject sweep tail (tier-6 argument echoes): a refusal about an
    ARGUMENT echoes the argument — distinct wrong inputs must produce
    distinguishable payloads."""
    from weft.api import Weft
    from weft.task import Task

    # task.command missing names the fields that WERE given
    with pytest.raises(WeftError) as ei:
        Task.from_dict({"site": "local"})
    assert ei.value.hints["given_fields"] == ["site"]
    # array echoes the got value (validate runs inside from_dict)
    with pytest.raises(WeftError) as ei:
        Task.from_dict({"command": "true", "site": "local", "array": 0})
    assert "got 0" in ei.value.detail

    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    # unknown kernel target echoes the kernel id
    r = w.ensure_available({"kernel": "krn_nope1234"}, {"pypi": ["idna"]})
    assert r["error"] == "task.invalid"
    assert "krn_nope1234" in r["detail"]
    # same-site route probe echoes the site
    r2 = w.site_route_probe("local", "local")
    assert r2["error"] == "task.invalid" and "'local'" in r2["detail"]
    # actor validation echoes the rejected value
    with pytest.raises(WeftError) as ei:
        w.as_actor("bad\x01actor")
    assert "got" in ei.value.hints
