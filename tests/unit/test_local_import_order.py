"""Use-before-local-import is a whole-function outage that greps clean:
a `from x import y` ANYWHERE in a function makes `y` function-local, so
a use ABOVE the import raises UnboundLocalError on every call — while
the module-level import of the same alias keeps linters and readers
calm. Instance: kernel.py appended `hermetic_interpreter_lines as
_hermetic_lines` to a local import BELOW the new use site, and every
kernel_start crashed for three commits (the surface's only behavioral
driver was outside the targeted sets). Same defect family as the
_vocab-fold parameter test: an edit that parses, imports, and only
fails when the verb is DRIVEN.

Rule: for each function, no Load of an alias may precede the FIRST
local import binding that alias (min-line rule — branch-local
re-imports each preceded by their own use sites pass)."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "weft"


def test_no_use_before_local_import():
    offences = []
    for p in sorted(SRC.rglob("*.py")):
        tree = ast.parse(p.read_text(), filename=str(p))
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef,
                                     ast.AsyncFunctionDef))]:
            first_import: dict[str, int] = {}
            for n in ast.walk(fn):
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    for a in n.names:
                        alias = a.asname or a.name.split(".")[0]
                        first_import[alias] = min(
                            first_import.get(alias, n.lineno), n.lineno)
            if not first_import:
                continue
            for n in ast.walk(fn):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    ln = first_import.get(n.id)
                    if ln and n.lineno < ln:
                        offences.append(
                            f"{p.relative_to(SRC)}:{n.lineno} in "
                            f"{fn.name}(): {n.id!r} used before its "
                            f"local import at line {ln}")
    assert not offences, (
        "use-before-local-import (UnboundLocalError on every call):\n"
        + "\n".join(offences))
