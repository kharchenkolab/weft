"""Step 3: forgiving solves — one call instead of a conflict-retry loop,
with hard pins never silently touched."""

import pytest

from weft.api import Weft

pytestmark = pytest.mark.solver


@pytest.fixture
def w(tmp_path, pixi_bin):
    return Weft(tmp_path / "ws", pixi_bin=pixi_bin)


def test_soft_constraint_is_relaxed_and_reported(w):
    spec = {"name": "soft", "deps": {"conda": [
        "python =3.12",
        "xz ==4.999.9?",          # soft: impossible, but only a preference
    ]}}
    strict = w.env_ensure(spec)                 # default: no relaxing
    assert strict["error"] == "env.solve_conflict"

    r = w.env_ensure(spec, relax="soft")
    assert "env_id" in r, r
    assert r["relaxed"][0]["dep"] == "xz ==4.999.9"
    assert r["relaxed"][0]["relaxed_to"] == "xz"
    assert r["relaxed"][0]["got"]                # the version it actually got
    assert "still fully pinned" in r["note"]
    # the result IS fully pinned — adaptiveness was in the path, not the lock
    assert w.env_status(r["env_id"])["summary"]["reproducibility"] \
        == "fully-pinned"
    kinds = [e["kind"] for e in w.events_poll(0, 200)["events"]]
    assert "env.relaxed" in kinds


def test_hard_pins_are_never_relaxed(w):
    """The invariant: a substrate must not silently drop the version the
    science depends on."""
    r = w.env_ensure({"name": "hard", "deps": {"conda": [
        "python =3.12", "xz ==4.999.9"]}}, relax="soft")
    assert r["error"] == "env.solve_conflict"
    assert "trailing '?'" in r["hints"]["relax"]   # tells you how to opt in


def test_relax_is_a_noop_when_it_already_solves(w):
    r = w.env_ensure({"name": "fine", "deps": {"conda": ["xz >=5?"]}},
                     relax="soft")
    assert "env_id" in r and "relaxed" not in r


def test_soft_marker_does_not_change_identity_when_satisfiable(w):
    """A '?' is a solver hint, not part of the constraint: if the pin holds,
    the EnvID is the same as the hard-pinned spec's."""
    a = w.env_ensure({"name": "x", "deps": {"conda": ["xz >=5"]}})
    b = w.env_ensure({"name": "x", "deps": {"conda": ["xz >=5?"]}},
                     relax="soft")
    assert a["env_id"] == b["env_id"]
