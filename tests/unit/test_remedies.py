"""One test per remedy PATH: a remedy is a contract about when its
advice applies, and each gate below replays the misdirection it
exists to prevent (the census's 9 lane-blanket sites; the r-signac
thread's two live misdirections first)."""

from weft.remedies import (cran_overlay_note, cran_realize_note,
                           fingerprint_list_tree, julia_realize_note,
                           move_base, packed_cran_note, revise_no_spec,
                           snapshot_verify_failed, solve_conflict)


def _regions(*markers):
    return [{"marker": m, "lines": "..."} for m in markers]


# ---- the two live misdirections (th594060f7 items 1-note and 5) ------

def test_solve_conflict_bare_name_never_suggests_soft_pins():
    """The r-signac case: the NAME does not exist on the channel."""
    got = solve_conflict("No candidates were found for weft-no-pkg")
    assert "does not exist" in got
    assert 'relax="soft"' not in got


def test_solve_conflict_versioned_pin_keeps_the_soft_lever():
    """The eval-adapt case (xz ==4.999.9) that caught the first gate
    being too coarse: the VERSION does not exist — dropping the pin
    solves, so relax="soft" is exactly right and must stay."""
    got = solve_conflict("No candidates were found for xz ==4.999.9")
    assert 'relax="soft"' in got
    assert "pinned version was not found" in got


def test_solve_conflict_bare_message_versioned_pin_via_spec():
    """Solvers sometimes echo only the name — the SPEC still knows the
    caller pinned a version; the pins discriminate."""
    got = solve_conflict("No candidates were found for xz",
                         user_pins=["xz ==4.999.9"])
    assert 'relax="soft"' in got
    got2 = solve_conflict("No candidates were found for xz",
                          user_pins=["xz"])
    assert 'relax="soft"' not in got2


def test_solve_conflict_real_conflict_keeps_soft_pin_door():
    got = solve_conflict("these packages are incompatible: a ==1, a ==2")
    assert 'relax="soft"' in got


def test_cran_note_dep_unavailable_is_not_networking():
    got = cran_realize_note(_regions("r_dep_unavailable"),
                            "dependencies 'x' are not available")
    assert "repo/name" in got
    assert "air-gapped" not in got


def test_cran_note_syslib_names_the_build_deps_lever():
    got = cran_realize_note(_regions("syslib"), "zlib.h: No such file")
    assert "build_deps" in got


def test_cran_note_network_only_on_network_markers():
    got = cran_realize_note([], "curl: (6) Could not resolve host")
    assert "air-gapped" in got


def test_cran_note_default_points_at_evidence():
    got = cran_realize_note([], "something else entirely")
    assert "error_regions" in got


# ---- the extends_env door (four-copies incident) ---------------------

def test_move_base_discriminates_both_ways():
    assert "re-ensure with `extends`" in move_base(True)
    shut = move_base(False)
    assert "re-ensure with `extends`" not in shut
    assert "adopt a newer published version" in shut


def test_revise_no_spec_discriminates_adopted_rows():
    assert "re-ensure from the original spec" == revise_no_spec(False)
    got = revise_no_spec(True)
    assert "re-adopt" in got and "re-ensure from the original" not in got


# ---- the remaining gated notes ---------------------------------------

def test_overlay_note_native_advice_needs_evidence():
    assert "cannot be layered" in cran_overlay_note(_regions("syslib"))
    assert "cannot be layered" not in cran_overlay_note([])


def test_julia_note_network_gated():
    assert "needs network" in julia_realize_note("connection timed out")
    assert "needs network" not in julia_realize_note(
        "ERROR: LoadError: something")


def test_packed_cran_toolchain_advice_needs_compiler_evidence():
    assert "toolchain" in packed_cran_note(_regions("compiler_missing"))
    assert "toolchain" not in packed_cran_note([])


def test_fingerprint_note_only_for_old_shim_shapes():
    assert fingerprint_list_tree("list-tree: unrecognized arguments")
    assert fingerprint_list_tree("No such file or directory") is None
    assert fingerprint_list_tree("Connection refused") is None


def test_snapshot_advice_needs_unportable_paths():
    got = snapshot_verify_failed(["/home/u/local-src"])
    assert got and "session_run_installer" in got
    assert snapshot_verify_failed([]) is None
    assert snapshot_verify_failed(None) is None


def test_cran_no_candidates_names_and_truncates():
    from weft.remedies import cran_no_candidates
    got = cran_no_candidates(["WrongName"], "2026-01-01")
    assert "WrongName" in got and "2026-01-01" in got
    assert "case-sensitive" in got and "r-<name>" in got
    many = cran_no_candidates([f"pkg{i}" for i in range(12)], None)
    assert "pkg7" in many and "pkg9" not in many and "…" in many


def test_strategy_refusals_name_the_site():
    """Subject sweep: strategy refusals carried no site identity."""
    import pytest as _pt

    from weft.errors import WeftError
    from weft.strategy import select_strategy
    with _pt.raises(WeftError) as ei:
        select_strategy({"internet": False, "runtimes": {}},
                        prefer="squashfs", site="beam")
    assert ei.value.hints.get("site") == "beam"


def test_julia_conflict_names_the_package():
    from weft.solvers import _julia_solve_error
    e = _julia_solve_error(
        "Unsatisfiable requirements detected for package DataFrames",
        ["DataFrames"])
    assert e.hints.get("package") == "DataFrames"
    assert "DataFrames" in e.detail


def test_ranked_namespace_ambiguity_names_the_names():
    import pytest as _pt

    from weft.errors import WeftError
    from weft.spec import ranked_namespace
    with _pt.raises(WeftError) as ei:
        ranked_namespace([("somepkg", {})], ["cran", "pypi"])
    assert ei.value.hints.get("ambiguous") == ["somepkg"]
    assert "somepkg" in ei.value.detail
