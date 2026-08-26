"""The RESTORE path is a deployment shape (asks-plan structural gap 3):
every test registers fresh, so anything done only at register_site was
structurally untested on restored estates — the shim propagated by
hash for NEW registrations only, and deployed estates carried v10
silently across a bump (aba2, two live workspaces). Bootstrap now runs
at FIRST REAL USE per adapter (ensure_bootstrap_once at every shim
call), healing restored estates in one marker+sha round-trip."""

from pathlib import Path

from weft.api import Weft


def test_restored_estate_heals_a_stale_shim(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local",
                    {"root": str(tmp_path / "site"),
                     "pixi_source": pixi_bin}, tools="skip")
    shim = tmp_path / "site" / "bin" / "weft-shim"
    original = shim.read_bytes()
    # forge a deployed-estate bump gap: the site carries an OLD shim
    shim.write_text("#!/bin/sh\necho '{\"shim_version\": 1}'\n")
    w.close()

    w2 = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")  # RESTORE
    r = w2._adapter("local").shim(["version"])
    assert shim.read_bytes() == original          # healed by sha at use
    assert r.rc == 0
    w2.close()


def test_first_use_heal_is_once_per_adapter(tmp_path, pixi_bin,
                                            monkeypatch):
    w = Weft(tmp_path / "ws2", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local",
                    {"root": str(tmp_path / "site2"),
                     "pixi_source": pixi_bin}, tools="skip")
    a = w._adapter("local")
    calls = []
    orig = a.ensure_bootstrap
    monkeypatch.setattr(a, "ensure_bootstrap",
                        lambda: calls.append(1) or orig())
    a.shim(["version"])
    a.shim(["version"])
    assert len(calls) <= 1        # register already checked; never twice
    w.close()
