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
