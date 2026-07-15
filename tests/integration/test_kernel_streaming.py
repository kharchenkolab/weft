"""Kernel block output streams LIVE (user-model ask: aba's interactive
lane tails .out at an offset — the file must grow while the block runs,
not arrive in one burst at completion)."""

import time

import pytest

from weft.api import Weft


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def test_block_output_grows_while_running(w):
    k = w.kernel_start("local", "python")["kernel_id"]
    adapter = w.adapters["local"]
    jobdir = w.store.get_kernel(k)["jobdir"]
    # implicit prints (no flush=True): exercises the throttled tee path
    r = w.kernel_exec(
        k, "import time\n"
           "for i in range(8):\n"
           "    print('tick', i)\n"
           "    time.sleep(0.25)\n", wait=False)
    n = r["block"]
    saw_partial = None
    for _ in range(100):
        time.sleep(0.1)
        if adapter.file_exists(f"{jobdir}/blocks/{n:04d}.rc"):
            break
        try:
            body = adapter.read_file(
                f"{jobdir}/blocks/{n:04d}.out").decode()
        except Exception:
            continue
        ticks = body.count("tick")
        if 0 < ticks < 8:
            saw_partial = ticks       # grew BEFORE completion
    done = w.kernel_poll(k, n, timeout=30)
    assert done.get("rc") == 0, done
    assert saw_partial, "output never appeared before the block finished"
    final = adapter.read_file(f"{jobdir}/blocks/{n:04d}.out").decode()
    assert final.count("tick") == 8            # contents unchanged, complete

    # interrupt semantics untouched: rc=130 + marker on the stream
    r2 = w.kernel_exec(k, "import time\ntime.sleep(60)\n", wait=False)
    time.sleep(1.0)
    w.kernel_interrupt(k)
    done2 = w.kernel_poll(k, r2["block"], timeout=30)
    assert done2.get("rc") == 130, done2
    err = adapter.read_file(
        f"{jobdir}/blocks/{r2['block']:04d}.err").decode()
    assert "[interrupted]" in err
    w.kernel_stop(k)
