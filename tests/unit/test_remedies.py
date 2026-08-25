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

def test_solve_conflict_no_candidates_never_suggests_soft_pins():
    got = solve_conflict('No candidates were found for "weft-no-pkg".')
    assert "does not exist" in got
    assert "relax" not in got.lower() or "cannot help" in got


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
