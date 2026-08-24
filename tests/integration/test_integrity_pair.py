"""Eight-asks round A — the integrity pair. Both are "the env/task lies
about its state" defects:

A1 (ask 1): pixi stages conda post-link scripts and never runs them —
conda-meta records the package installed while its payload may not
exist (bioconductor data packages; DESeq2 fails to load three steps
later). Realize now detects staged scripts and REFUSES by default
(env.post_link_scripts, packages + levers named); site policy
post_link:"warn" accepts with a loud event. Running the scripts is
deliberately not offered — several download unpinned content, which
would fork the EnvID's meaning between realizations.

A2 (ask 5): consumers inferred "weft activated the env" from
CONDA_PREFIX — clobberable shell-hook fallout, with "activation
failed" indistinguishable from "submitted without an env". cmd.sh now
opens with the activation guard: proof of activation exports
WEFT_ENV_ID (weft's fact) + WEFT_PREFIX; failure exits 78 BEFORE user
code and is classified env.activation_failed, never a user-code
failure."""

import json
from pathlib import Path

import pytest

from weft.api import Weft
from weft.classify import classify_log
from weft.errors import WeftError
from weft.realize import _post_link_check
from weft.runner_util import activation_guard_lines


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


# ---------------------------------------------------------------- A1

class _FakeStore:
    def __init__(self):
        self.events = []

    def emit(self, kind, **kw):
        self.events.append({"kind": kind, **kw})


class _FakeAdapter:
    name = "fake"

    def __init__(self, tmp, scripts=()):
        self.root = tmp
        bindir = tmp / "envroot/.pixi/envs/default/bin"
        bindir.mkdir(parents=True, exist_ok=True)
        for s in scripts:
            (bindir / s).write_text("#!/bin/sh\ncurl example.com\n")
        self.cmds = []

    def path(self, rel):
        return str(self.root / rel) if not rel.startswith("/") else rel

    def run_cmd(self, cmd, timeout=60):
        self.cmds.append(cmd)
        import subprocess
        r = subprocess.run(["sh", "-c", cmd], capture_output=True,
                           text=True)

        class R:
            pass
        R.rc, R.out, R.err = r.returncode, r.stdout, r.stderr
        return R


def test_post_link_refuses_by_default_and_names_everything(tmp_path):
    ad = _FakeAdapter(tmp_path, scripts=[
        ".bioconductor-genomeinfodbdata-post-link.sh",
        ".bioconductor-org.hs.eg.db-post-link.sh"])
    (tmp_path / "envroot").mkdir(exist_ok=True)
    (tmp_path / "envroot/.weft-ready").write_text("{}")
    store = _FakeStore()
    with pytest.raises(WeftError) as ei:
        _post_link_check("env:v1:x", ad, "envroot", "prefix", store, {})
    e = ei.value
    assert e.code == "env.post_link_scripts" and e.stage == "realize"
    assert e.hints["packages"] == [
        "bioconductor-genomeinfodbdata", "bioconductor-org.hs.eg.db"]
    assert "post_install" in e.hints["levers"]
    assert "rm $PREFIX" in e.hints["levers"]["post_install"]
    assert "unpinned" in e.hints["note"]          # why no "run" lever
    # the ready marker was consumed: no cache-hit past the refusal
    assert not (tmp_path / "envroot/.weft-ready").exists()


def test_post_link_warn_policy_accepts_with_event(tmp_path):
    ad = _FakeAdapter(tmp_path, scripts=[".pkgx-post-link.sh"])
    store = _FakeStore()
    _post_link_check("env:v1:x", ad, "envroot", "prefix", store,
                     {"policy": {"post_link": "warn"}})
    (ev,) = store.events
    assert ev["kind"] == "realize.post_link_unrun"
    assert ev["packages"] == ["pkgx"]


def test_post_link_clean_prefix_silent(tmp_path):
    ad = _FakeAdapter(tmp_path, scripts=[])
    store = _FakeStore()
    _post_link_check("env:v1:x", ad, "envroot", "prefix", store, {})
    assert store.events == []


def test_post_link_acknowledged_by_removal(tmp_path):
    """The post_install lever: delivering the payload and REMOVING the
    staged script is the acknowledgment — the check goes silent."""
    ad = _FakeAdapter(tmp_path, scripts=[".pkgy-post-link.sh"])
    (tmp_path / "envroot/.pixi/envs/default/bin/.pkgy-post-link.sh").unlink()
    _post_link_check("env:v1:x", ad, "envroot", "prefix", _FakeStore(), {})


# ---------------------------------------------------------------- A2

def test_guard_lines_shape():
    lines = activation_guard_lines("env:v1:abc")
    text = "\n".join(lines)
    assert "WEFT_ENV_ID=env:v1:abc" in text
    assert "exit 78" in text
    assert activation_guard_lines(None) == []     # envless: no marker


def test_classify_puts_activation_first():
    got = classify_log("Traceback (most recent call last):\n"
                       "weft: activation did not take (CONDA_PREFIX=unset)")
    assert got["signature"] == "activation-failed"


def test_envless_task_has_no_marker(w):
    r = w.task_submit({"command": "echo \"marker=[$WEFT_ENV_ID]\" > m.txt",
                       "outputs": ["m.txt"], "site": "local"})
    assert w.runner.wait(r["job_id"], 120)["state"] == "DONE"
    jd = Path(w.adapters["local"].path(f"jobs/{r['job_id']}"))
    assert (jd / "m.txt").read_text().strip() == "marker=[]"


@pytest.mark.solver
def test_env_task_exports_weft_env_id(w):
    env = w.env_ensure({"name": "guard-env",
                        "deps": {"conda": ["python =3.12"]}})
    assert "error" not in env, env
    r = w.task_submit({"command": "echo \"$WEFT_ENV_ID\" > id.txt; "
                                  "echo \"$WEFT_PREFIX\" > px.txt",
                       "env": env["env_id"], "outputs": ["id.txt",
                                                         "px.txt"],
                       "site": "local"})
    assert w.runner.wait(r["job_id"], 600)["state"] == "DONE"
    jd = Path(w.adapters["local"].path(f"jobs/{r['job_id']}"))
    assert (jd / "id.txt").read_text().strip() == env["env_id"]
    assert (jd / "px.txt").read_text().strip()      # prefix real


@pytest.mark.solver
def test_clobbered_activation_fails_typed_before_user_code(w):
    """Hostile-ambient: the activation is sabotaged (env_vars clobber
    CONDA_PREFIX after activate.sh ran — task env_vars land in cmd.sh
    BEFORE... no: guard is FIRST in cmd.sh, so sabotage must come from
    the sourced side). Simulate the real failure: corrupt the env's
    activate.sh content post-realize via a task whose activation
    sources a broken script — done by pointing a second task at the
    realized env after truncating its activate.sh."""
    env = w.env_ensure({"name": "sab-env",
                        "deps": {"conda": ["python =3.12"]}})
    assert "error" not in env, env
    ok = w.task_submit({"command": "true", "env": env["env_id"],
                        "site": "local"})
    assert w.runner.wait(ok["job_id"], 600)["state"] == "DONE"
    # sabotage the realized activation (ambient mutation weft doesn't own)
    loc = w.store.get_realization(env["env_id"], "local")["location"]
    ad = w.adapters["local"]
    ad.write_file(f"{loc}/activate.sh",
                  b"unset CONDA_PREFIX\n")          # activation 'runs', sets nothing
    bad = w.task_submit({"command": "echo SHOULD-NOT-RUN > ran.txt",
                         "env": env["env_id"], "site": "local"},
                        force=True)
    got = w.runner.wait(bad["job_id"], 300)
    assert got["state"] == "FAILED"
    err = json.loads(w.store.get_job(bad["job_id"])["error"]
                     if isinstance(got.get("error"), str) else
                     json.dumps(got.get("error") or {})) \
        if got.get("error") else got
    text = json.dumps(got)
    assert "env.activation_failed" in text, text
    jd = Path(ad.path(f"jobs/{bad['job_id']}"))
    assert not (jd / "ran.txt").exists()            # user code never ran


def test_every_build_lane_runs_post_link_check():
    """The gap-1 pin, SECOND iteration (consumer audits #1 and #2,
    both 2026-08-24): first a docstring claimed a check that did not
    exist; then the fix wired it through an OPTIONAL store=None
    parameter and the publish lane — which builds the published packs,
    the motivating incident's own artifacts — silently disarmed it by
    not passing store. Their critique of v1 of THIS TEST also stands:
    a source-grep of the callee's body pins the callee; the claim
    'every build lane routes through the check' is about CALLERS.
    Held three ways now: (1) `store` is KEYWORD-REQUIRED — Python
    itself refuses any caller that forgets (fail-open defaults on a
    safety check are the disarm-by-default class); (2) the callee
    calls the check unconditionally, ordered after post_install and
    before the image write; (3) the publish caller is driven
    BEHAVIORALLY in test_publish_lane_arms_post_link_check."""
    import inspect

    from weft import realize
    sig = inspect.signature(realize._build_squashfs)
    store_p = sig.parameters["store"]
    assert store_p.default is inspect.Parameter.empty, \
        "store grew a default — callers can silently disarm the check"
    assert store_p.kind == inspect.Parameter.KEYWORD_ONLY
    tail = inspect.getsource(realize.ensure_realization)
    assert "_post_link_check" in tail                # main build tail
    sq = inspect.getsource(realize._build_squashfs)
    assert "_post_link_check" in sq, \
        "squashfs staging lost the post-link check"
    assert "if store is not None" not in sq          # unconditional
    assert sq.index("_run_post_install") < sq.index("_post_link_check")
    # the staging layout follows the build branch — both strategies
    # must be discriminated, or the packed branch globs the wrong dir
    assert '"prefix" if internet else "packed"' in sq
    # before the IMAGE WRITE (the invocation flags — "mksquashfs" the
    # word first appears in the capability lookup near the top)
    assert sq.index("_post_link_check") < sq.index("-noappend")


def test_publish_lane_arms_post_link_check(w, tmp_path, monkeypatch):
    """BEHAVIORAL caller coverage (their exact ask): drive the real
    env_publish entry point and assert the store the check needs
    actually arrives at _build_squashfs. A spy replaces the builder,
    records its kwargs, and stops the flow with a sentinel — no
    mksquashfs required. Red on the audited commit: store arrived as
    None there."""
    import weft.realize as realize_mod
    # an offline env row (local repodata-only channel; no artifacts
    # are fetched by pixi lock)
    import hashlib as _h
    import json as _json
    chan = tmp_path / "chan"
    import platform as _platform
    import sys as _sys
    sub = ("osx-arm64" if _sys.platform == "darwin"
           and _platform.machine() == "arm64" else "linux-64")
    for d in (chan / sub, chan / "noarch"):
        d.mkdir(parents=True)
    fn = "pubpkg-1.0-h0_0.conda"
    (chan / sub / "repodata.json").write_text(_json.dumps(
        {"info": {"subdir": sub}, "packages": {}, "packages.conda": {
            fn: {"name": "pubpkg", "version": "1.0", "build": "h0_0",
                 "build_number": 0, "subdir": sub, "depends": [],
                 "sha256": _h.sha256(fn.encode()).hexdigest()}}}))
    (chan / "noarch" / "repodata.json").write_text(_json.dumps(
        {"info": {"subdir": "noarch"}, "packages": {},
         "packages.conda": {}}))
    env = w.env_ensure({"name": "pub-env", "channels": [chan.as_uri()],
                        "platforms": [sub],
                        "deps": {"conda": ["pubpkg ==1.0"]}})
    assert "error" not in env, env
    # forge squashfs capability so publish reaches the builder
    row = w.store.get_site("local")
    caps = dict(row.get("capabilities") or {})
    caps["squashfs"] = {"mksquashfs": True, "squashfuse": True,
                        "dev_fuse": True, "userns": True}
    w.store.set_capabilities("local", caps)

    seen = {}

    def spy(env_id, env_row, adapter, rel, modules, modules_init, caps,
            pack_tools, emit, staging_rel=None, *, store,
            site_config=None):
        seen["store"] = store
        seen["site_config"] = site_config
        raise WeftError("internal.error", "spy stop", stage="realize")

    monkeypatch.setattr(realize_mod, "_build_squashfs", spy)
    out = w.env_publish(env["env_id"], "local",
                        str(tmp_path / "pub-tree"), "pack", "1.0")
    assert out.get("error"), out                 # the sentinel surfaced
    assert seen, "publish never reached the builder"
    assert seen["store"] is w.store              # the check is ARMED
    assert isinstance(seen["site_config"], dict)
