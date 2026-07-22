"""Round D (2026-07 sweep): environment hygiene. The control plane
parses tool output — it runs under LC_ALL=C; user jobs must NOT inherit
that (a hard C locale breaks unicode in user python), keeping only
LC_MESSAGES=C so their logs stay classifiable. Module loads demand a
load PRODUCT (Tcl EM 3.x errors at exit 0)."""

import subprocess

import pytest

from weft.adapters.base import ShimResult
from weft.adapters.local import LocalAdapter
from weft.adapters.ssh import SSHAdapter
from weft.realize import module_prelude


# ── control plane: C locale everywhere it parses ───────────────────────────

def test_control_plane_locale_is_pinned(tmp_path):
    assert "LC_ALL=C" in SSHAdapter(
        "s", "unused", str(tmp_path), transport="local")._env_prefix()
    assert LocalAdapter("l", tmp_path)._env()["LC_ALL"] == "C"


def test_control_plane_prints_point_decimals(tmp_path, monkeypatch):
    """The darwin shim emitted comma-decimal floats under the mac's
    locale — invalid JSON read as site.unreachable. Pin the behavior,
    not just the variable."""
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    for ad in (LocalAdapter("l", tmp_path / "A"),
               SSHAdapter("s", "unused", str(tmp_path / "B"),
                          transport="local")):
        assert ad.run_cmd("printf '%.1f' 1.5").out == "1.5"


# ── user jobs: un-inherit LC_ALL, keep classifiable diagnostics ────────────

def test_job_wrapper_uninherits_lc_all(tmp_path, pixi_bin):
    from weft.api import Weft
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    r = w.task_submit({"command":
                       'echo "${LC_ALL:-unset}|$LC_MESSAGES" > o.txt',
                       "outputs": ["o.txt"], "site": "local"})
    assert w.runner.wait(r["job_id"], 120)["state"] == "DONE"
    got = (tmp_path / "site/jobs" / r["job_id"] / "o.txt").read_text().strip()
    assert got == "unset|C"


def test_job_env_vars_override_locale(tmp_path, pixi_bin):
    from weft.api import Weft
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    r = w.task_submit({"command": 'echo "$LC_ALL" > o.txt',
                       "outputs": ["o.txt"],
                       "env_vars": {"LC_ALL": "C.UTF-8"},
                       "site": "local"})
    assert w.runner.wait(r["job_id"], 120)["state"] == "DONE"
    got = (tmp_path / "site/jobs" / r["job_id"] / "o.txt").read_text().strip()
    assert got == "C.UTF-8"


# ── module load: the product is the proof ──────────────────────────────────

_TCL_EM_SILENT = """
module() {
    if [ "$1" = load ]; then
        echo "ERROR:102: Tcl command execution failed" >&2
        return 0
    fi
    return 0
}
"""

_HONEST = """
WEFT_TEST_LOADED=""
module() {
    if [ "$1" = load ]; then
        WEFT_TEST_LOADED="$WEFT_TEST_LOADED $2"
        return 0
    fi
    if [ "$1" = list ]; then
        for m in $WEFT_TEST_LOADED; do echo "$m"; done
        return 0
    fi
    return 0
}
"""


def _run_prelude(fake: str, modules: list) -> subprocess.CompletedProcess:
    script = fake + module_prelude(modules) + "\necho SURVIVED"
    return subprocess.run(["sh", "-c", script],
                          capture_output=True, text=True, timeout=30)


def test_tcl_em_silent_failure_is_caught():
    """module load errors to stderr, exits ZERO on Tcl EM 3.x — the
    || guard is inert; the missing load product must stop the job
    (else it runs against host toolchains with the env's name on the
    manifest — wrong-provenance class)."""
    r = _run_prelude(_TCL_EM_SILENT, ["cuda/12.2"])
    assert r.returncode == 90
    assert "no load product" in r.stderr
    assert "SURVIVED" not in r.stdout


def test_honest_module_load_passes():
    r = _run_prelude(_HONEST, ["cuda/12.2"])
    assert r.returncode == 0, r.stderr
    assert "SURVIVED" in r.stdout


def test_versionless_ask_matches_versioned_expansion():
    """`module load gcc` lists as gcc/12.2 — the product check must
    accept the expansion, not demand a literal match."""
    expanded = _HONEST.replace('WEFT_TEST_LOADED="$WEFT_TEST_LOADED $2"',
                               'WEFT_TEST_LOADED="$WEFT_TEST_LOADED $2/12.2"')
    r = _run_prelude(expanded, ["gcc"])
    assert r.returncode == 0, r.stderr


# ── scheduler estimates: naive local time says so ──────────────────────────

def test_estimated_start_names_its_timezone():
    from weft.adapters.slurm import SlurmAdapter
    ad = SlurmAdapter("hpc", "login", "/site/root", user="u")
    ad.run_cmd = lambda cmd, timeout=120.0: ShimResult(
        0, "sbatch: Job 123 to start at 2026-07-22T21:00:00 "
           "using 8 processors on nodes n01 in partition main", "")
    got = ad.estimate_start({"cpus": 8})
    assert got["estimated_start"] == "2026-07-22T21:00:00"
    assert "scheduler-local" in got["timezone"]
