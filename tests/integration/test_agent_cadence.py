"""Machine-cadence tests (post-bug2 doctrine): every surface an agent
drives gets one test that drives it as fast as the API allows, with
strict per-operation assertions. Human-paced tests structurally cannot
catch percent-level races — bug2 sat at 1-3%% per block under fixture
timing and 30-60%% in the field. Kernel block cadence lives in
test_kernel_block_race.py; this file covers the other agent surfaces."""

from weft.api import Weft

SERVER = ("python3 -c \"import http.server, os; "
          "http.server.HTTPServer(('127.0.0.1', int(os.environ['WEFT_PORT'])), "
          "http.server.SimpleHTTPRequestHandler).serve_forever()\"")


def _w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def test_task_burst_zero_think_time(tmp_path, pixi_bin):
    """Six distinct tasks submitted back-to-back before any completes;
    each must land with its OWN output (no cross-talk, no silent empty)."""
    w = _w(tmp_path, pixi_bin)
    jobs = [(i, w.task_submit({"command": f"echo t-{i} > out.txt",
                               "outputs": ["out.txt"],
                               "site": "local"})["job_id"])
            for i in range(6)]
    for i, jid in jobs:
        job = w.runner.wait(jid, 180)
        assert job["state"] == "DONE", (i, job["error"])
        out = next(o for o in job["manifest"]["outputs"]
                   if o["path"] == "out.txt")
        assert out["preview"]["lines"] == [f"t-{i}"], (i, out)


def test_array_fanout_at_speed(tmp_path, pixi_bin):
    """8-element fan-out; every element's own output checked — an element
    that silently ran nothing (bug2 class) fails here, not just a count."""
    w = _w(tmp_path, pixi_bin)
    r = w.task_submit({"command": "echo el-$WEFT_ARRAY_INDEX > out.txt",
                       "outputs": ["out.txt"], "site": "local", "array": 8})
    for sub in r["jobs"]:
        job = w.runner.wait(sub["job_id"], 600)
        assert job["state"] == "DONE", job["error"]
        out = next(o for o in job["manifest"]["outputs"]
                   if o["path"] == "out.txt")
        assert out["preview"]["lines"] == [f"el-{sub['array_index']}"], sub
    st = w.array_status(r["group"])
    assert st["done"] == 8 and st.get("failed", 0) == 0


def test_service_churn(tmp_path, pixi_bin):
    """Start -> ready -> stop, twice back-to-back: readiness probing and
    teardown must not leak state into the next cycle."""
    w = _w(tmp_path, pixi_bin)
    for cycle in range(2):
        r = w.service_start("local", {"command": SERVER},
                            ports=[18500 + cycle], ready_timeout=30)
        assert r["state"] == "ready", (cycle, r)
        assert w.service_status(r["service_id"])["state"] == "ready"
        out = w.service_stop(r["service_id"])
        assert out["state"] == "stopped", (cycle, out)
