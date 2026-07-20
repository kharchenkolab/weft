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
        tr = w.kernel_transcript(k)
        silent = [e for e in tr
                  if e["rc"] == 0 and not e.get("out_tail", "").strip()
                  and "print" in e.get("code", "")]
        assert not silent, silent
    finally:
        w.kernel_stop(k)


def test_block_cadence_local(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    _drive_cadence(w, "local")


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
