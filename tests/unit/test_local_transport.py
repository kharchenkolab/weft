"""SlurmAdapter transport="local" (user-model ask): the controller runs
ON the submit node — every primitive is a direct subprocess, no
ssh-to-self (GSSAPI/2FA-only sites refuse that hop). Scheduler logic is
transport-blind; these pin the primitive surface. Reality-tested on
cbe.next 2026-07-15 (job jb_061fce844d3c via real sbatch)."""

import pytest

from weft.adapters.slurm import SlurmAdapter
from weft.errors import WeftError


@pytest.fixture
def adapter(tmp_path):
    a = SlurmAdapter("here", "localhost", str(tmp_path / "root"),
                     transport="local")
    a.ensure_bootstrap()
    return a


def test_primitives_run_without_ssh(adapter):
    r = adapter.run_cmd("echo local-$((6*7))")
    assert (r.rc, r.out.strip()) == (0, "local-42")
    adapter.write_file("d/f.txt", b"payload")
    assert adapter.read_file("d/f.txt") == b"payload"
    assert adapter.file_exists("d/f.txt") and not adapter.file_exists("no")
    assert adapter.shim(["version"]).json()["shim_version"] >= 5


def test_staging_is_local_link(adapter):
    ep = adapter.transfer_endpoint()
    assert ep["method"] == "local-link"        # same machine: no wire
    assert ep["cas_root"].endswith("/cas")


def test_hop_check_reports_no_chain(adapter):
    hops = adapter.hop_check()
    assert hops == [{"hop": "local", "ok": True,
                     "note": "controller runs on this site; no ssh chain"}]


def test_unknown_transport_refused(tmp_path):
    with pytest.raises(WeftError) as e:
        SlurmAdapter("x", "h", str(tmp_path), transport="carrier-pigeon")
    assert e.value.hints["known"] == ["ssh", "local"]


def test_env_prefix_still_applies(adapter):
    r = adapter.run_cmd("echo $WEFT_ROOT; echo $PIXI_CACHE_DIR")
    lines = r.out.splitlines()
    assert lines[0] == adapter.root
    assert lines[1].endswith("cache/pixi")
