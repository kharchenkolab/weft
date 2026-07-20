"""bug2 reality shape: back-to-back one-liner blocks through the real
kernel protocol — every block's stdout AND side effects must land.

The field failure (aba, ssh site): SSHAdapter.write_file published .code
files with `cat > dest`, the driver's exists->read loop caught the
truncate window, exec'd an empty block, and reported rc=0 — stdout lost,
side effects (a makedirs + file write) silently skipped. Rates: 3-6 of
10 one-line prints per run; 6/6 for the first block at agent cadence."""

import pytest

from weft.api import Weft

N = 10


def assert_no_silent_blocks(w, kernel_id):
    """A block that should have printed but has rc=0 and empty out is the
    bug2 signature — silent false success. Reusable across kernel tests."""
    silent = [e for e in w.kernel_transcript(kernel_id, last=100)
              if e["rc"] == 0 and not e.get("out_tail", "").strip()
              and ("print" in e.get("code", "") or "cat(" in e.get("code", ""))]
    assert not silent, silent


def _drive_cadence(w, site):
    k = w.kernel_start(site, "python")["kernel_id"]
    try:
        # the original probe: N one-line prints, submitted back-to-back
        for i in range(N):
            r = w.kernel_exec(k, f"print('blk-{i} ok', {i}*7)", timeout=120)
            assert r["rc"] == 0, (i, r)
            assert r["out"].strip() == f"blk-{i} ok {i * 7}", (i, r)
        # the nastier variant: a silently-skipped block loses SIDE EFFECTS
        r = w.kernel_exec(
            k, "import os\nos.makedirs('d', exist_ok=True)\n"
               "open('d/f.txt', 'w').write('persisted')", timeout=120)
        assert r["rc"] == 0, r
        r2 = w.kernel_exec(k, "print(open('d/f.txt').read())", timeout=120)
        assert r2["rc"] == 0 and r2["out"].strip() == "persisted", r2
        # weft's own durable record agrees: no block ran silent
        assert_no_silent_blocks(w, k)
    finally:
        w.kernel_stop(k)


def test_block_cadence_local(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    _drive_cadence(w, "local")


def test_block_burst_async_local(tmp_path, pixi_bin):
    """The truest agent cadence: submit ALL blocks wait=False before the
    driver has run any (controller writes .code N+1 while the driver
    executes N), then poll each — every output must land."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    k = w.kernel_start("local", "python")["kernel_id"]
    try:
        blocks = [w.kernel_exec(k, f"print('b-{i}', {i} * 3)",
                                wait=False)["block"] for i in range(N)]
        for i, b in zip(range(N), blocks):
            r = w.kernel_poll(k, b, timeout=120)
            assert r["rc"] == 0, (i, r)
            assert r["out"].strip() == f"b-{i} {i * 3}", (i, r)
        assert_no_silent_blocks(w, k)
    finally:
        w.kernel_stop(k)


def test_two_kernels_interleaved_local(tmp_path, pixi_bin):
    """Two kernels on one site, blocks interleaved at speed — no
    cross-talk between jobdirs, both transcripts complete."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    k1 = w.kernel_start("local", "python")["kernel_id"]
    k2 = w.kernel_start("local", "python")["kernel_id"]
    try:
        for i in range(5):
            r1 = w.kernel_exec(k1, f"print('k1-{i}')", timeout=120)
            r2 = w.kernel_exec(k2, f"print('k2-{i}')", timeout=120)
            assert r1["out"].strip() == f"k1-{i}", (i, r1)
            assert r2["out"].strip() == f"k2-{i}", (i, r2)
        assert_no_silent_blocks(w, k1)
        assert_no_silent_blocks(w, k2)
    finally:
        w.kernel_stop(k1)
        w.kernel_stop(k2)


@pytest.mark.docker
def test_block_cadence_ssh(tmp_path, pixi_bin, sshd_site):
    """The transport where the race actually fired: ssh latency widens
    the truncate window to milliseconds."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("beamlab", "ssh", {
        "host": sshd_site["host"], "port": sshd_site["port"],
        "user": sshd_site["user"], "ssh_opts": sshd_site["ssh_opts"],
        "root": sshd_site["root"], "pixi_source": pixi_bin})
    _drive_cadence(w, "beamlab")


@pytest.mark.docker
def test_block_cadence_ssh_wan(tmp_path, pixi_bin, sshd_site_wan):
    """Same cadence at FIELD timing (50ms netem): the profile under
    which the pre-fix code lost 30-60%% of blocks. A non-atomic publish
    regression fires here on nearly every block."""
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("farlab", "ssh", {
        "host": sshd_site_wan["host"], "port": sshd_site_wan["port"],
        "user": sshd_site_wan["user"], "ssh_opts": sshd_site_wan["ssh_opts"],
        "root": sshd_site_wan["root"], "pixi_source": pixi_bin})
    _drive_cadence(w, "farlab")
