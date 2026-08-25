"""The tool registry cannot drift silently: every public Weft method is
either in PUBLIC_TOOLS (the MCP/agent surface) or on the explicit
exclusion list below WITH a reason. A kernel_peek-shaped omission —
defined, documented, invisible — fails here instead of waiting for a
sharp-eyed reader."""

from weft.api import PUBLIC_TOOLS, Weft

EXCLUDED = {
    "events_subscribe": "push callbacks cannot cross the tool boundary; "
                        "events_poll is the tool-shaped path",
    "env_ensure_dry_run": "exclusion inherited, not yet deliberate — "
                          "flagged 2026-07-15; add to PUBLIC_TOOLS or "
                          "justify here",
    "resolve_run_file": "reached through data_register(run=, rel=) and "
                        "{'run','rel'} task inputs — one tool surface "
                        "for one concept",
    "as_actor": "embedder-scoped attribution contextmanager — a per-call "
                "tool parameter would let an agent spoof the audit trail "
                "(design refusal, footprint round 26)",
}


def _public_methods():
    return {n for n, v in vars(Weft).items()
            if callable(v) and not n.startswith("_")}


def test_every_public_method_is_registered_or_excluded():
    public = _public_methods()
    missing = public - set(PUBLIC_TOOLS) - set(EXCLUDED)
    assert not missing, (
        f"public Weft methods invisible to the tool surface: "
        f"{sorted(missing)} — add to PUBLIC_TOOLS or EXCLUDED (with a "
        f"reason)")


# Verbs with NO fast-lane test today (docker/solver suites don't count:
# the fast lane is what gates pushes). This list may only SHRINK — a
# NEW verb, or an edit to an old one, must land with fast-lane coverage.
# Lesson (cran round): a stray line broke array_result outright and TWO
# full green lanes certified it, because its only test was docker-marked
# — a green lane proves exactly what it measures, nothing more.
FAST_LANE_UNCOVERED = {
    "doctor", "env_find_near", "env_gpu_hint",
    "env_repair", "env_revise", "env_unpublish", "gc_packages",
    "job_node_exec", "module_list", "site_associations", "site_footprint",
    "site_load", "site_probe", "site_probe_deep", "site_route_probe",
    "site_teardown",
}


def test_every_public_verb_has_fast_lane_coverage():
    import re
    from pathlib import Path
    tests_dir = Path(__file__).resolve().parents[1]
    blob = "\n".join(
        p.read_text() for p in tests_dir.rglob("test_*.py")
        if not re.search(r"pytestmark\s*=.*mark\.(docker|solver)",
                         p.read_text()[:2000]))
    uncovered = {v for v in PUBLIC_TOOLS
                 if not re.search(rf"\.{re.escape(v)}\s*\(", blob)}
    new_gaps = uncovered - FAST_LANE_UNCOVERED
    assert not new_gaps, (
        f"public verbs with NO fast-lane test: {sorted(new_gaps)} — "
        "add a test; the push-gating lane cannot see these break")
    stale = FAST_LANE_UNCOVERED - uncovered
    assert not stale, (
        f"now covered — remove from FAST_LANE_UNCOVERED: {sorted(stale)}")


def test_every_public_verb_has_a_docstring():
    """Generated catalogs surface the docstring (first paragraph
    especially) — an empty one ships an EMPTY CONTRACT for the verb.
    Found 14 empty at once (task_submit and data_fetch among them: a
    live model burned a diagnose-and-resubmit cycle discovering the
    outputs semantics by experiment — aba leftovers note, item 4)."""
    import inspect
    empty = [v for v in PUBLIC_TOOLS
             if not (inspect.getdoc(getattr(Weft, v)) or "").strip()]
    assert not empty, f"public verbs with no docstring: {empty}"


def test_registry_and_exclusions_are_coherent():
    public = _public_methods()
    both = set(EXCLUDED) & set(PUBLIC_TOOLS)
    assert not both, f"excluded AND registered: {sorted(both)}"
    ghosts = set(PUBLIC_TOOLS) - public
    assert not ghosts, f"registered but not defined: {sorted(ghosts)}"
    stale = set(EXCLUDED) - public
    assert not stale, f"exclusions for methods that no longer exist: " \
                      f"{sorted(stale)}"
