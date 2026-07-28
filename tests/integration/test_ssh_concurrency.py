"""N concurrent channels on one mux master (aba three-tabs note): the
shared connection must absorb a channel storm — overflow falls back to
direct connections, and the master survives every failure shape."""

import threading
import time

import pytest

from weft.api import Weft


@pytest.mark.docker
def test_channel_storm_shares_one_master(tmp_path, pixi_bin, sshd_site):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("beam", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin})
    ad = w.adapters["beam"]
    ad.run_cmd("true")                    # one warm master
    results, guard = [], threading.Lock()

    def lane(i):
        try:
            r = ad.run_cmd("sleep 1 && echo ok", timeout=60)
            with guard:
                results.append("ok" if "ok" in r.out else f"rc={r.rc}")
        except Exception as e:            # noqa: BLE001 — collect all
            with guard:
                results.append(f"ERR {str(e)[:80]}")

    N = 14                                # > sshd MaxSessions default 10
    ts = [threading.Thread(target=lane, args=(i,)) for i in range(N)]
    t0 = time.monotonic()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    wall = time.monotonic() - t0
    assert results.count("ok") == N, results
    # not serialized: 14 x sleep-1 must land well under 14s
    assert wall < 10, wall
    # and the master is still alive for the next call
    assert "ok" in ad.run_cmd("echo ok").out
