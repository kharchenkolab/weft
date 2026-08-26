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

_SOFT_LEVER = (
    "mark the negotiable pins SOFT with a trailing "
    "'?' (e.g. \"scipy ==1.14.1?\") and call "
    "env_ensure(..., relax=\"soft\"): weft relaxes "
    "only those, reports what it gave up, and the "
    "result is still fully pinned.")


def solve_conflict(solver_message: str,
                   user_pins: list[str] | None = None) -> str:
    """env.solve_conflict's suggestion, gated on the solver's words AND
    the caller's pins — the first gate was too coarse and the eval-adapt
    motivating-incident test caught it the first solver-lane run (R1):

    - no candidates for a BARE NAME (r-signac on conda-forge): the
      package does not exist there — softening cannot conjure it;
      remedy is spelling/channel/lane.
    - no candidates for a VERSIONED spec (xz ==4.999.9): the package
      exists, the PINNED VERSION does not — relax="soft" is exactly
      the right lever (dropping the pin solves), so the door STAYS.
    """
    import re
    low = (solver_message or "").lower()
    if "no candidates" in low:
        m = re.search(r"no candidates were found for\s+([^\n]+)", low)
        subject = (m.group(1).strip().rstrip(".") if m else "")
        parts = subject.split(None, 1)
        name = parts[0] if parts else ""
        constraint = parts[1] if len(parts) > 1 else ""
        versioned = bool(re.search(r"[0-9<>=!~]", constraint)) \
            and constraint.strip() != "*"
        if not versioned and user_pins and name:
            # the message sometimes echoes only the name — the SPEC
            # knows whether the caller pinned a version
            from .spec import split_constraint
            for pin in user_pins:
                n, c = split_constraint(pin)
                if n.lower() == name and c not in ("", "*"):
                    versioned = True
                    break
        if versioned:
            return (
                f"the pinned version was not found on the spec's "
                f"channels ({subject or 'see solver_message'}) — the "
                f"package may exist under other versions: correct the "
                f"pin, or " + _SOFT_LEVER + " If the NAME is also "
                "wrong, softening will not help — check spelling and "
                "channels.")
        return (
            "the package does not exist on the spec's channels — "
            "check the spelling, add the channel that carries it "
            "(bioconductor-* lives on bioconda), or use the ecosystem "
            "lane that does (deps.cran / deps.pypi). Softening version "
            "pins cannot help here.")
    # uv's pypi not-found shape (ask 31's replay, milopy: this same
    # stderr rode the parse arm into internal.error "do not edit pins"
    # pre-corpus; now it lands here and must NAME the package)
    m = re.search(r"because\s+(\S+)\s+was not found in the package"
                  r"\s+registry", low)
    if m:
        name = m.group(1).strip("'\"`")
        return (
            f"pypi package '{name}' was not found in the package "
            "registry — check the exact spelling on pypi.org; a yanked "
            "or metadata-broken release can also resolve as not-found. "
            "conda-forge may carry it in deps.conda; a package that "
            "ships only as a release-asset wheel needs the URL-pin "
            f"lane (deps.pypi: '{name} @ https://…whl#sha256=<hex>').")
    return (_SOFT_LEVER + " Or relax/remove "
            "the conflicting pin named in solver_message.")


def cran_no_candidates(names: list[str], snapshot: str | None) -> str:
    """The cran solve's missing-package refusal, NAMING its subject
    (aba2 ask 32): 124 distinct wrong names produced 124 identical
    'unsatisfiable against the repository set' refusals and a 3-round
    misdiagnosis — a refusal that names the packages converts that to
    a 10-second fix. FOUR applicability paths, each with a lever: the
    first version shipped only three, and the solver lane's extra-repo
    scenario (right name, undeclared repo) lost its r_repositories
    steer for a day."""
    shown = ", ".join(names[:8]) + (" …" if len(names) > 8 else "")
    return (
        f"no candidates for: {shown} — CRAN names are case-sensitive "
        f"(check exact spelling on cran.r-project.org); a package "
        f"younger than the snapshot"
        + (f" ({snapshot})" if snapshot else "")
        + " is invisible to it (raise cran_snapshot); a package from a "
          "lab/Posit/drat repo needs that repo declared in "
          "r_repositories; conda-forge may carry it as r-<name> in "
          "deps.conda; github sources spell owner/repo@ref.")


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
        # REALIZE surface: build_deps is a session-only lever — naming
        # it here was the bug5-A2 class (advice an env spec cannot
        # follow). deps.conda is the spec's lever, and it is CORRECT,
        # not a workaround: the compiled .so links the library at
        # runtime. Compilers are weft's errand (toolchain retry).
        return ("the build is missing a system library on this site — "
                "see failure_class/missing_system: add its conda "
                "package to deps.conda (the built .so links it at "
                "runtime, so it is a real dependency); compilers are "
                "weft's own toolchain — a toolchain.failed event says "
                "if provisioning was the problem.")
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
        # same bug5-A2 sweep: "(or build_deps …)" pointed a realize
        # surface at a session-only lever
        return ("the package needs a native library the parent lacks "
                "— it cannot be layered: add that conda package to "
                "the parent env's deps.conda (the runtime .so must "
                "resolve from the parent every consumer sources).")
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
        # the verb takes source=<path> (weft content-addresses and
        # re-stages it) — the old text said inputs=[...], a TASK-
        # vocabulary kwarg this verb refuses at bind time (bug5-A2
        # sweep: the sibling copy at run_installer's own refusal
        # already spelled it right; two copies, one dead)
        return ("an installer step depends on a local path that will "
                "not exist at realize time: re-run it via "
                "session_run_installer(..., source=\"<path>\") so "
                "weft content-addresses and re-stages the source, or "
                "make the step self-contained "
                f"(unportable: {unportable[:4]})")
    return None
