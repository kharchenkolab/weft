"""The solver-stderr classifier's conformance CORPUS: real captured
output, one file per shape (author-written fixtures could not express
the overlapping-marker theft that misrouted aba2's missing-package
error into internal.error 'do not edit pins' — ask 31). Format: line 1
`# expect: <code>`, leading `#` lines are provenance, the rest is the
verbatim stderr. RULE: every future misclassification incident appends
its stderr here as a new file — the corpus only grows."""

from pathlib import Path

import pytest

from weft.lock import _classify_solve_failure
from weft.spec import EnvSpec

CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "stderr_corpus"

SPEC = EnvSpec.from_dict({"name": "corpus", "platforms": ["linux-64"],
                          "deps": {"conda": ["python =3.12.*"],
                                   "pypi": ["milopy"]}})


def _cases():
    for f in sorted(CORPUS.glob("*.txt")):
        lines = f.read_text().splitlines()
        assert lines[0].startswith("# expect: "), f
        expect = lines[0].split(": ", 1)[1].strip()
        body = "\n".join(l for l in lines if not l.startswith("#"))
        yield pytest.param(body, expect, id=f.stem)


@pytest.mark.parametrize("stderr,expect", list(_cases()))
def test_corpus_classifies(stderr, expect):
    err = stderr.strip()
    tail = "\n".join(err.splitlines()[-30:])
    e = _classify_solve_failure(err, tail, err.lower(), SPEC,
                                cache_dir=None, cache_why="default")
    assert e.code == expect, (e.code, e.detail)


def test_corpus_is_not_empty_and_covers_every_arm():
    codes = set()
    for f in CORPUS.glob("*.txt"):
        codes.add(f.read_text().splitlines()[0].split(": ", 1)[1].strip())
    assert {"env.solve_conflict", "env.solve_failed", "internal.error",
            "task.invalid"} <= codes, codes
