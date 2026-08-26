"""Duplicate test-file basenames abort the WHOLE fast lane at
collection (rootdir import mode; no __init__.py in tests/): pytest
exits 2 before running anything, so every test the lane exists to run
— including the one that would have caught the live bug — silently
does not run, while explicit-path targeted invocations keep passing.
Second incident (test_vocabulary was renamed for this; then
test_hermetic_interpreters.py landed in unit/ beside integration's,
and the full lane died at collection for three commits while
kernel_start was broken — integration's test_kernel_is_hermetic would
have caught it on first run). This pin fails in ANY collection that
includes it, so a targeted unit run reports the collision before the
full lane hits it."""

from collections import Counter
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]


def test_test_file_basenames_are_unique():
    names = Counter(p.name for p in TESTS.rglob("test_*.py"))
    dupes = {n: sorted(str(p.relative_to(TESTS))
                       for p in TESTS.rglob(n))
             for n, c in names.items() if c > 1}
    assert not dupes, (
        "duplicate test basenames abort full-lane collection "
        f"(exit=2, nothing runs): {dupes}")
