"""bug5-A2 class pin: advice must not name verb kwargs the verb
refuses. The motivating instance: snapshot_verify_failed advised
`session_run_installer(..., inputs=[...])` — a TASK-vocabulary kwarg
the tool wrapper rejects at bind time (tool.bad_arguments) — while
the sibling refusal spelled `source=` correctly: two copies, one
dead. This scans EVERY string constant in src/weft for
`<public_verb>(... kwarg=...)` shapes and binds each named kwarg
against the live signature, so a signature change or a new advice
string cannot re-open the class.

(The deeper halves of the class — session levers printed on realize
surfaces and vice versa — are pinned semantically per-remedy in
test_remedies.py; a mechanical surface-reachability scan would need
call-graph facts strings do not carry.)"""

import ast
import inspect
import re
from pathlib import Path

import weft
from weft.api import Weft

SRC = Path(weft.__file__).parent

VERBS = {name: fn for name, fn in inspect.getmembers(Weft)
         if not name.startswith("_") and callable(fn)}

_CALL_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\(([^()]*)\)")
_KWARG_RE = re.compile(r"(?<![\w'\"])([a-z_][a-z0-9_]*)=(?!=)")


def _bad_kwargs_in(text: str) -> list[str]:
    bad = []
    for m in _CALL_RE.finditer(text):
        verb, args = m.group(1), m.group(2)
        fn = VERBS.get(verb)
        if fn is None:
            continue
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        if any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
            continue
        for km in _KWARG_RE.finditer(args):
            if km.group(1) not in sig.parameters:
                bad.append(f"{verb}(... {km.group(1)}=)")
    return bad


def test_the_motivating_string_would_be_caught():
    """Red-proof built in: the exact pre-fix advice trips the scan."""
    assert _bad_kwargs_in(
        "re-run it via session_run_installer(..., inputs=[...])"
    ) == ["session_run_installer(... inputs=)"]
    assert _bad_kwargs_in(
        "re-run it via session_run_installer(..., source=\"<p>\")") == []


def test_every_advice_string_binds():
    offenders = []
    for py in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and \
                    isinstance(node.value, str) and "(" in node.value:
                for b in _bad_kwargs_in(node.value):
                    offenders.append(
                        f"{py.relative_to(SRC)}:{node.lineno} {b}")
    assert not offenders, (
        "advice names kwargs the verb refuses at bind time "
        "(bug5-A2 class):\n" + "\n".join(offenders))
