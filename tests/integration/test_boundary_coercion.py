"""aba2 asks 33/34/34b + the ssh-config note — the serialization-
artifact battery for the ONE tool boundary.

Replayed transcripts (verbatim shapes from their day-one keep flow):
- run_retain(include='["summaries/"]'): a JSON-stringified list rode
  fnmatch AS A STRING → "selection matched no files", cause invisible
  across three refusals.
- include='["*name.csv", ...]': the stringified pattern parsed as an
  fnmatch CHARACTER CLASS and matched 30 unrelated files — garbage
  selection, "successful" retain. With the boundary door it can never
  reach fnmatch: it coerces (valid JSON) or refuses naming the shape.
- session_install(pypi='["pkg"]'): the old refusal named the first
  character ("cannot parse dependency string: '['"), not the problem.
- kernel_start(env_id='{...}'): reported as a missing inline-resolve
  sibling (ask 34b) — the resolve EXISTED since L5; the stringified
  dict was the actual bug, and the boundary door fixes it.
- register_site({"ssh": {"host": ...}}): KeyError traceback where a
  door belonged.
"""

import json
from pathlib import Path

import pytest

from weft.api import Weft, tool


@pytest.fixture
def w(tmp_path, pixi_bin):
    w = Weft(tmp_path / "ws", pixi_bin=pixi_bin)
    w.register_site("local", "local", {"root": str(tmp_path / "site"),
                                       "pixi_source": pixi_bin})
    w.runner.poll_interval = 0.2
    return w


def _run(w, cmd):
    r = w.task_submit({"command": cmd, "site": "local"})
    assert w.runner.wait(r["job_id"], 120)["state"] == "DONE"
    return r["job_id"]


# ── ask 33: the coercion door, end-to-end ──────────────────────────────────

def test_stringified_include_coerces_and_echoes(w):
    """The incident's exact call shape now WORKS, and says what it
    fixed — the agent learns the artifact existed without a refusal."""
    jid = _run(w, "mkdir -p summaries && echo s > summaries/stats.txt")
    r = w.run_retain(jid, include='["summaries/"]', background=False,
                     dest="@workspace")
    assert r["state"] == "done" and r["files"] == 1, r
    assert "coerced_params" in r and "include" in r["coerced_params"]
    assert (Path(r["location"]["path"]) / "summaries/stats.txt").exists()


def test_charclass_string_can_never_reach_fnmatch(w):
    """The garbage-match variant: '["*name.csv", ...]' is VALID JSON,
    so the door coerces it — fnmatch sees real patterns, never the
    bracket-leading string that doubles as a character class. The
    selection is exactly the patterns' intent."""
    jid = _run(w, "mkdir -p d && echo a > d/runname.csv && "
                  "echo b > other.txt && echo c > name.csv")
    r = w.run_retain(jid, include='["*name.csv", "other.txt"]',
                     background=False, dest="@workspace")
    assert r["state"] == "done", r
    kept = {f.name for f in Path(r["location"]["path"]).rglob("*")
            if f.is_file() and f.name != ".weft-run.json"}
    assert kept == {"runname.csv", "name.csv", "other.txt"}, kept
    assert "coerced_params" in r


def test_unparseable_bracket_string_refuses_naming_the_shape(w):
    r = w.run_retain("job_x", include='["*name.csv"')   # truncated JSON
    assert r["error"] == "tool.bad_arguments"
    assert "array" in r["detail"] and "include" in r["detail"], \
        "the refusal names the SHAPE and the param, not a character"
    assert r["hints"]["param"] == "include"
    assert "serialization" in r["detail"]


def test_stringified_dict_param_coerces(w):
    """The object arm: task_submit(task='{...}') is the same artifact
    one door over."""
    r = w.task_submit('{"command": "echo hi", "site": "local"}')
    assert "error" not in r, r
    assert "coerced_params" in r and "task" in r["coerced_params"]
    assert w.runner.wait(r["job_id"], 120)["state"] == "DONE"


def test_session_install_stringified_pypi_crosses_the_boundary(w):
    """Their session_install transcript: pypi='["pkg"]' burned four
    call shapes in 23s against "cannot parse dependency string: '['".
    Post-door, the string coerces BEFORE the verb — the call fails on
    the nonexistent session (proof the dep parser never saw the
    artifact), not on a bracket."""
    r = w.session_install("ses_nonexistent", pypi='["idna"]')
    assert r["error"] == "task.invalid"
    assert "unknown session" in r["detail"], r
    assert "'['" not in r["detail"], \
        "the per-item parser must never see the serialization artifact"


# ── ask 34b: the stringified spec vs kernel_start ──────────────────────────

def test_stringified_spec_reaches_the_inline_resolve(w):
    """Reported as 'kernel_start lacks task_submit's inline-resolve';
    the resolve has existed since L5 — the agent's spec dict arrived
    STRINGIFIED and read as an EnvID. The door now lands it in the
    resolve as a real dict."""
    captured = {}

    def fake_ensure(spec_or_id, **kw):
        captured["spec"] = spec_or_id
        return {"error": "env.solve_failed", "detail": "stub"}

    w.env_ensure = fake_ensure          # instance attr wins over class
    spec = {"name": "t", "deps": {"conda": ["python =3.12"]}}
    r = w.kernel_start("local", env_id=json.dumps(spec))
    assert captured["spec"] == spec, \
        "the boundary must hand the resolve a REAL dict"
    assert r["error"] == "env.solve_failed", "ensure verdict passes through"


# ── never-coerce conformance ───────────────────────────────────────────────

class _Probe:
    store = None

    @tool
    def verb(self, items: list[str] | None = None,
             spec: dict | None = None, command: str = ""):
        return {"items": items, "spec": spec, "command": command}


def test_str_annotated_params_are_never_touched():
    p = _Probe()
    shell = '[ -f x ] && echo "{ok}"'      # legitimate bracket-leading str
    r = p.verb(command=shell)
    assert r["command"] == shell and "coerced_params" not in r


def test_real_containers_pass_untouched_and_unechoed():
    p = _Probe()
    r = p.verb(items=["a"], spec={"k": 1})
    assert r["items"] == ["a"] and r["spec"] == {"k": 1}
    assert "coerced_params" not in r


def test_array_string_on_dict_only_param_passes_through():
    """A '['-string on a dict-annotated param is NOT coerced (the
    verb's own refusal owns that mismatch) — the door only acts where
    the parse is unambiguous for the annotation."""
    p = _Probe()
    r = p.verb(spec='["a"]')
    assert r["spec"] == '["a"]' and "coerced_params" not in r


def test_object_string_coerces_on_dict_param():
    p = _Probe()
    r = p.verb(spec='{"k": 1}')
    assert r["spec"] == {"k": 1} and "spec" in r["coerced_params"]


# ── ask 34: zero-match teaches ─────────────────────────────────────────────

def test_zero_match_names_near_misses(w):
    """The reported shape verbatim: the kernel wrote into a subfolder;
    a bare basename matched nothing; the old refusal echoed the
    selection with zero WHY."""
    jid = _run(w, "mkdir -p outdir && echo x > outdir/table.csv")
    r = w.run_retain(jid, include=["table.csv"], background=False,
                     dest="@workspace")
    assert r["error"] == "data.missing"
    assert r["hints"]["near_misses"]["table.csv"] == ["outdir/table.csv"]
    assert "grammar" in r["hints"] and "outdir/" not in r["detail"]
    assert r["hints"]["files_in_run"] >= 1


def test_zero_match_without_near_miss_samples_the_tree(w):
    jid = _run(w, "echo x > real-output.txt")
    r = w.run_retain(jid, include=["zzz-nothing-close"],
                     background=False, dest="@workspace")
    assert r["error"] == "data.missing"
    assert "near_misses" not in r["hints"]
    assert "real-output.txt" in r["hints"]["sample_paths"], \
        "with nothing close, the tree itself is the teaching"


def test_near_miss_owner_shapes():
    from weft.retain import RetainManager
    entries = [{"path": "outdir/table.csv"}, {"path": "logs/x.log"},
               {"path": "cmd.sh", "scaffold": True}]
    nm = RetainManager._near_misses(entries, ["table.csv", "cmd.sh",
                                              "TABLE"])
    assert nm["table.csv"] == ["outdir/table.csv"]
    assert "cmd.sh" not in nm, "scaffold never suggested"
    assert nm["TABLE"] == ["outdir/table.csv"], \
        "case-insensitive substring fallback"


# ── the ssh-config door ────────────────────────────────────────────────────

def test_nested_ssh_config_refused_typed(w, tmp_path):
    r = w.register_site("x", "ssh", {"ssh": {"host": "login.example"},
                                     "root": str(tmp_path / "r")})
    assert r["error"] == "task.invalid", \
        "a config-shape guess must hit a door, not a KeyError traceback"
    assert "host" in r["detail"]
    assert r["hints"]["found_nested"] == {
        "host": "config['ssh']['host']"}
    assert "example" in r["hints"]


def test_ssh_config_missing_both_names_both(w):
    r = w.register_site("y", "ssh", {})
    assert r["error"] == "task.invalid"
    assert "host" in r["detail"] and "root" in r["detail"]
    assert r["hints"]["required"] == ["host", "root"]


def test_local_missing_root_refused(w):
    r = w.register_site("z", "local", {"pixi_source": "/x"})
    assert r["error"] == "task.invalid" and "root" in r["detail"]
