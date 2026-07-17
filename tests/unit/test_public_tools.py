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


def test_registry_and_exclusions_are_coherent():
    public = _public_methods()
    both = set(EXCLUDED) & set(PUBLIC_TOOLS)
    assert not both, f"excluded AND registered: {sorted(both)}"
    ghosts = set(PUBLIC_TOOLS) - public
    assert not ghosts, f"registered but not defined: {sorted(ghosts)}"
    stale = set(EXCLUDED) - public
    assert not stale, f"exclusions for methods that no longer exist: " \
                      f"{sorted(stale)}"
