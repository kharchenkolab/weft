"""Remedy text is CODE, not prose — one owner per remedy.

The motivating incident (remedy census 2026-08-25): "re-ensure with
`extends`" — a door that raises parent-spec-not-found on adopt-only
workspaces — was pasted as prose at FOUR sites; the #118 sweep fixed
two and certified "both"; the other two shipped dark because pasted
prose drifts past greps. A remedy that lives here is a function taking
the FACTS it discriminates on: a new landing site is a call, a copy is
impossible, and the discrimination is the signature.

The census's second finding gates the rest: 9 sites attached one
unconditional remedy to failures with several distinguishable causes
(the cran "air-gapped/network" note on a dependency-name failure; the
soft-pin suggestion on a package that does not exist). Every function
below that takes evidence (regions/text) is marker-GATED — and each
gate is pinned by tests/unit/test_remedies.py, one test per path
(failure payloads are contracts).
"""

from __future__ import annotations

# network markers: one owner (lock.py consumes these for its
# solve-failure classification; the gated notes below reuse them)
NETWORK_MARKERS = ("connection", "timed out", "dns", "network",
                   "fetch repodata", "could not resolve",
                   "unable to access index")


def _region_markers(regions: list[dict] | None) -> set[str]:
    return {r.get("marker") for r in (regions or [])}


def _looks_network(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in NETWORK_MARKERS)


# ------------------------------------------------------------ extends_env

def move_base(parent_spec_present: bool) -> str:
    """The extends_env freeze remedy — EVERY landing site calls this
    (the four-copies incident). The free-the-base door only opens when
    the parent's spec body is stored; on an adopt-only workspace the
    honest doors are a newer published version or bundle_import."""
    return (
        "`extends_env` freezes the base on purpose. To move it, "
        + ("re-ensure with `extends` (the parent's SPEC hash) for "
           "a free re-solve and a full prefix."
           if parent_spec_present else
           "adopt a newer published version of the pack, or "
           "bundle_import the env from a workspace that holds its "
           "spec (this adopt-only workspace has no spec body to "
           "re-solve from)."))


def revise_no_spec(adopted: bool) -> str:
    """revise()'s no-spec refusal: 're-ensure from the original spec'
    is a shut door when the row was adopted — there IS no spec here
    (found during the L1 round's zero-add-child investigation)."""
    if adopted:
        return ("this env was adopted without a spec body — re-adopt "
                "from a republished tree (modern sidecars carry the "
                "spec), or bundle_import from a workspace that has it")
    return "re-ensure from the original spec"


# ------------------------------------------------------------ solve lane

def solve_conflict(solver_message: str) -> str:
    """env.solve_conflict's suggestion, gated on the solver's own
    words: 'no candidates' means the name/version does not EXIST on the
    channels — softening a version window cannot conjure it (the
    r-signac agent was told to relax pins on a package conda-forge
    simply does not carry)."""
    low = (solver_message or "").lower()
    if "no candidates" in low:
        return (
            "the package (or the pinned version) does not exist on the "
            "spec's channels — check the spelling, add the channel that "
            "carries it (bioconductor-* lives on bioconda), or use the "
            "ecosystem lane that does (deps.cran / deps.pypi). "
            "Softening version pins cannot help here.")
    return (
        "mark the negotiable pins SOFT with a trailing "
        "'?' (e.g. \"scipy ==1.14.1?\") and call "
        "env_ensure(..., relax=\"soft\"): weft relaxes "
        "only those, reports what it gave up, and the "
        "result is still fully pinned. Or relax/remove "
        "the conflicting pin named in solver_message.")


# --------------------------------------------------------- realize lanes

def cran_realize_note(regions: list[dict] | None, text: str) -> str:
    """The cran realize failure note, gated on the evidence — the
    unconditional 'air-gapped/network' text steered the r-signac agent
    toward networking on a dependency-NAME failure."""
    marks = _region_markers(regions)
    if "r_dep_unavailable" in marks:
        return ("R reports dependencies 'not available' — a repo/name "
                "problem, not networking: the package (or a dep) is not "
                "in the configured repositories. Check the name, add "
                "the repo that carries it (cran_repos= / "
                "r_repositories), or use conda-forge r-<name> in "
                "deps.conda.")
    if "syslib" in marks or "compiler_missing" in marks:
        return ("the build is missing a compiler or system library on "
                "this site — see failure_class/missing_system: "
                "build_deps supplies headers/libs; a toolchain-less "
                "site needs cxx-compiler and friends via the conda "
                "layer.")
    if _looks_network(text):
        return ("cran realization needs network from the install point "
                "in v1; on air-gapped sites prefer conda-forge "
                "r-<name> packages or build R packages as tasks.")
    return ("read error_regions / log_path for the causal line; the "
            "install is incremental, so a fixed spec re-realizes from "
            "the frontier.")


def cran_overlay_note(regions: list[dict] | None) -> str:
    """The overlay-specific arm: the native-library advice only when
    the evidence says so."""
    marks = _region_markers(regions)
    if "syslib" in marks or "compiler_missing" in marks:
        return ("the package needs a native library/toolchain the "
                "parent lacks — it cannot be layered: add that conda "
                "package to the parent env (or build_deps for "
                "compile-time-only needs).")
    if "r_dep_unavailable" in marks:
        return ("R reports dependencies 'not available' — a repo/name "
                "problem: check the name or add the repo "
                "(cran_repos=).")
    return "read error_regions / log_path for the causal line."


def julia_realize_note(text: str) -> str:
    marks_net = _looks_network(text)
    if marks_net:
        return ("julia realization needs network from the install "
                "point in v1.")
    return ("read error_regions / log_path; Unsatisfiable/LoadError "
            "shapes are package-level, not networking.")


def packed_cran_note(regions: list[dict] | None) -> str:
    marks = _region_markers(regions)
    if "syslib" in marks or "compiler_missing" in marks:
        return ("packages build from source on the site; the conda "
                "layer must provide the toolchain (c-compiler, "
                "fortran-compiler, make).")
    return ("offline install failed before/outside compilation — read "
            "error_regions / log_path (archive integrity, disk, "
            "permissions are the usual causes).")


# ----------------------------------------------------------- misc lanes

def fingerprint_list_tree(err: str) -> str | None:
    """data_fingerprint's 'old shim' note fired on ANY nonzero rc —
    including bad paths and outages, which re-registering cannot fix.
    Only an unknown-subcommand shape earns it; otherwise no note (the
    raw error speaks)."""
    low = (err or "").lower()
    if ("unrecognized" in low or "unknown command" in low
            or "invalid choice" in low):
        return ("old site shims lack file-root list-tree — "
                "re-register the site to refresh")
    return None


def snapshot_verify_failed(unportable: list[str] | None) -> str | None:
    """session_snapshot's post-mint verify failure OVERWROTE the
    underlying error's hints (which may carry a correctly-gated
    syslib/network diagnosis) with installer-inputs advice — only
    earned when unportable installer paths actually exist."""
    if unportable:
        return ("an installer step depends on a local path that will "
                "not exist at realize time: register its sources "
                "(data_register) and re-run it via "
                "session_run_installer(..., inputs=[...]), or make "
                "the step self-contained "
                f"(unportable: {unportable[:4]})")
    return None
