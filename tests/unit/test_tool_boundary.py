"""The tool boundary is ONE owner with a pinned contract: every public
verb returns JSON-shaped data; WeftError, call-binding failures, and
internal crashes all cross as TYPED envelopes, never as raw python
exceptions (aba2 th594060f7 items 2+3: a cyclic envelope crashed every
consumer's json.dumps, and an unknown kwarg surfaced a bare TypeError).
The strict-envelope mode (WEFT_STRICT_ENVELOPES, set suite-wide in
conftest) makes every green test also certify its envelopes."""

import json

import pytest

from weft.api import PUBLIC_TOOLS, Weft, _bounded, tool
from weft.errors import CODES, WeftError


class _Store:
    def __init__(self):
        self.events = []

    def emit(self, kind, **kw):
        self.events.append((kind, kw))


class _Host:
    def __init__(self):
        self.store = _Store()


# a representative verb: required + defaulted params, docstring contract
def _verb(self, name, why="", timeout=120):
    """First paragraph is the contract shown on bad_arguments.

    Second paragraph must NOT appear there."""
    return {"ok": True, "name": name}


wrapped = tool(_verb)


def test_unknown_kwarg_answers_typed_with_the_live_signature():
    got = wrapped(_Host(), "x", bogus=1)
    assert got["error"] == "tool.bad_arguments"
    assert got["meaning"] == CODES["tool.bad_arguments"]
    assert got["hints"]["verb"] == "_verb"
    # the agent-facing signature: no `self`, defaults visible
    assert got["hints"]["signature"] == \
        "_verb(name, why='', timeout=120)"
    assert got["hints"]["contract"].startswith("First paragraph")
    assert "Second paragraph" not in got["hints"]["contract"]


def test_missing_required_arg_answers_typed():
    got = wrapped(_Host())
    assert got["error"] == "tool.bad_arguments"
    assert "name" in got["detail"]


def test_body_typeerror_is_NOT_misread_as_bad_arguments():
    """The discrimination pin: binding is checked BEFORE the call, so a
    TypeError raised inside the body classifies as internal.error — an
    agent must never be told to fix a call that was well-formed."""
    def broken(self, x):
        raise TypeError("body bug, not a binding problem")
    got = tool(broken)(_Host(), 1)
    assert got["error"] == "internal.error"
    assert "body bug" in got["hints"]["traceback_tail"]


def test_internal_crash_is_typed_and_loud():
    def crasher(self, x):
        return {}[x]                       # KeyError — a weft bug
    host = _Host()
    got = tool(crasher)(host, "missing")
    assert got["error"] == "internal.error"
    assert got["retryable"] is False or "retryable" in got
    assert "KeyError" in got["hints"]["traceback_tail"]
    assert got["hints"]["verb"] == "crasher"
    # loud: the bug also lands in the event stream
    kinds = [k for k, _ in host.store.events]
    assert "internal.error" in kinds


def test_crash_in_the_store_itself_still_returns_an_envelope():
    class _DeadStore:
        def emit(self, *a, **kw):
            raise RuntimeError("store is down")

    class _DeadHost:
        store = _DeadStore()

    def crasher(self, x):
        raise RuntimeError("boom")
    got = tool(crasher)(_DeadHost(), 1)
    assert got["error"] == "internal.error"


def test_wefterror_envelopes_as_before():
    def refuser(self):
        raise WeftError("task.invalid", "no", stage="infra",
                        hints={"k": "v"})
    got = tool(refuser)(_Host())
    assert got["error"] == "task.invalid"
    assert got["hints"] == {"k": "v"}
    assert got["meaning"] == CODES["task.invalid"]


def test_strict_mode_fails_the_test_on_a_cyclic_success_payload():
    """The consumer's cycle rode the RETURN path of a verb, not the
    raise path — sealing must cover returned values (conftest turns
    strict mode on for the whole suite, so this is live everywhere)."""
    def cyclist(self):
        d = {"hints": {}}
        d["hints"]["attempts"] = [{"error": {"hints": d["hints"]}}]
        return d
    with pytest.raises(AssertionError) as ei:
        tool(cyclist)(_Host())
    assert "cyclist" in str(ei.value)


def test_nonstrict_error_envelope_is_salvaged_typed(monkeypatch):
    """Production (no strict flag): a broken ERROR envelope must still
    cross the boundary as a typed internal.error carrying a repr of the
    original — never a raw json crash in the consumer."""
    monkeypatch.delenv("WEFT_STRICT_ENVELOPES", raising=False)

    def bad_hints(self):
        raise WeftError("task.invalid", "x", stage="infra",
                        hints={"unserializable": {1, 2}})
    got = tool(bad_hints)(_Host())
    assert got["error"] == "internal.error"
    assert "bad_hints" in got["hints"]["verb"]
    assert "unserializable" in got["hints"]["payload_repr"]
    json.dumps(got)                         # the salvage itself is clean


def test_nonstrict_success_payloads_are_not_taxed(monkeypatch):
    """Deliberate scope pin: without the strict flag, SUCCESS payloads
    skip the dumps check (production hot path) — the error side is
    always checked because a broken refusal is a double failure."""
    monkeypatch.delenv("WEFT_STRICT_ENVELOPES", raising=False)

    def odd(self):
        return {"payload": {1, 2}}          # not JSON, not an error
    got = tool(odd)(_Host())
    assert got["payload"] == {1, 2}


def test_every_public_verb_is_wrapped():
    """The loop at the bottom of api.py IS the boundary — this reads
    the _weft_tool marker (written since the decorator existed, never
    consumed until now) so the wrapping can never silently drift."""
    unwrapped = [v for v in PUBLIC_TOOLS
                 if not getattr(getattr(Weft, v), "_weft_tool", False)]
    assert not unwrapped, f"verbs outside the boundary: {unwrapped}"


def test_bounded_lever_paths():
    assert _bounded(600, 1, 3600, "timeout") == 600
    assert _bounded("90", 1, 3600, "timeout") == 90     # int-shaped str
    with pytest.raises(WeftError) as ei:
        _bounded(99999, 1, 3600, "timeout")
    assert ei.value.code == "task.invalid"
    assert ei.value.hints == {"min": 1, "max": 3600}
    with pytest.raises(WeftError) as ei:
        _bounded("soon", 1, 3600, "timeout")
    assert ei.value.hints == {"min": 1, "max": 3600}


@pytest.fixture
def w(tmp_path, pixi_bin):
    return Weft(tmp_path / "ws", pixi_bin=pixi_bin, resume="off")


def test_site_exec_timeout_is_a_bounded_lever(w):
    """Item 3's missing lever: the fixed 120s pushed a real rebuild out
    to raw ssh. In range: honored; out of range: typed refusal with the
    bounds — never a silent clamp (honest numbers)."""
    r = w.site_exec("nosite", "true", "test", timeout=99999)
    assert r["error"] == "task.invalid"
    assert r["hints"] == {"min": 1, "max": 3600}


def test_site_exec_rejects_unknown_kwarg_typed(w):
    """The reporter's exact call shape, through the real verb: an
    unknown kwarg must answer tool.bad_arguments with the signature."""
    r = w.site_exec("nosite", "true", "test", timeouts=300)
    assert r["error"] == "tool.bad_arguments"
    assert "timeout" in r["hints"]["signature"]


def test_session_exec_timeout_plumbs_through(w, monkeypatch):
    seen = {}
    monkeypatch.setattr(w, "_session_adapter", lambda sid: "adapter")

    def spy(sid, adapter, cmd, timeout=600):
        seen["timeout"] = timeout
        return {"rc": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(w.sessions, "exec", spy)
    w.session_exec("s", "true", timeout=1234)
    assert seen["timeout"] == 1234


def test_session_snapshot_verify_refuses_nonbool(w):
    """Verb-surface census: verify={...} — the vocabulary of
    session_install — is truthy, so it silently acted as plain True
    here, dropping the caller's postconditions."""
    r = w.session_snapshot("sess-none", verify={"loads": ["x"]})
    assert r["error"] == "task.invalid"
    assert "session_install" in r["detail"]
