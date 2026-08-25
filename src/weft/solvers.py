"""Solver registry: one resolver per packaging ecosystem (design-next §2).

A Solver turns declarative deps into a locked *layer* of the canonical
lock document, and knows how to realize that layer into an existing env
directory on a site. The conda+pypi pair is one layer (pixi solves them
together); further ecosystems (cran, julia, …) stack on top, each
appending its activation lines.

Operability contract (design-next §4): solve failures are WeftError
`env.solve_conflict` with normalized hints {ecosystem, solver_message,
user_pins}; cross-layer requirement violations are `env.layer_conflict`
naming both sides and the fix.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .errors import WeftError
from . import remedies as _remedies
from .evidence import extract_error_regions as _extract_regions


class Solver(Protocol):
    ecosystem: str
    # conda packages this layer needs present in the base layer (the
    # cross-layer contract, enforced generically before any solve)
    conda_requirements: tuple[str, ...]

    def solve(self, deps: list[str], spec, workdir: Path,
              conda_packages: dict | None = None) -> dict:
        """-> layer dict: {"records": [...sorted, hashed...],
        "native": <native lockfile text>, "requires": {...}}.
        conda_packages: {conda_name: version} resolved in the base layer
        on EVERY platform — layers delta against it (a package the conda
        layer already provides must not be re-installed from source;
        aba 1.2 measured 25/26 cran installs buying nothing)."""
        ...

    def realize_layer(self, layer: dict, adapter, env_rel: str,
                      progress=None, build_jobs: int | None = None) -> str:
        """Install the layer into the realized env dir on the site;
        return activation lines to append (e.g. R_LIBS exports).
        progress: optional callback(done=, total=) invoked periodically
        during long installs; build_jobs caps source-build parallelism."""
        ...

    def why(self, env_row: dict, package: str, workdir: Path) -> str:
        """Reverse-dependency explanation for a package in this layer."""
        ...


# R's OWN base packages — the STATIC list, not `installed.packages(
# priority=...)` of whatever R runs the solve: conda r-base ships no
# "recommended" packages, so the priority query answered differently per
# controller and the closure (the LOCK) inherited that nondeterminism.
# Recommended packages (Matrix, MASS, ...) are deliberately NOT here:
# they are real dependencies — the conda delta or the layer provides them.
BASE_R_PACKAGES = (
    "base", "compiler", "datasets", "grDevices", "graphics", "grid",
    "methods", "parallel", "splines", "stats", "stats4", "tcltk",
    "tools", "translations", "utils",
)


def ppm_platform_url(url: str, os_id: str, codename: str) -> str:
    """THE Posit-Package-Manager URL platformizer (one owner): normalize
    any /cran/ PPM URL to its plain (source) form, then insert the
    __linux__/<codename>/ binary segment ONLY when the site is a distro
    PPM actually builds for. The old constant hardcoded focal — on
    non-focal hosts PPM silently serves source tarballs (aba 1.2: 582 s
    of compiling that binaries would have skipped), and locks minted
    before this change carry the focal segment, so existing URLs are
    REWRITTEN here too, keeping identity (the snapshot date) intact."""
    import re
    m = re.match(r"(https://packagemanager\.posit\.co/cran)"
                 r"(?:/__linux__/[^/]+)?(/.*)?$", url)
    if not m:
        return url                     # not PPM: caller's URL is law
    plain = m.group(1) + (m.group(2) or "")
    if os_id in ("ubuntu", "debian") \
            and codename in PPM_BINARY_CODENAMES:
        return (f"{m.group(1)}/__linux__/{codename}" + (m.group(2) or ""))
    return plain


# distro codenames PPM publishes binary trees for — an unsupported
# codename in the URL 404s the whole repository, which is strictly worse
# than the honest source fallback
PPM_BINARY_CODENAMES = {"focal", "jammy", "noble", "bullseye", "bookworm"}


def site_ppm_url(url: str, adapter) -> str:
    """ppm_platform_url against THIS site's distro, detected once per
    adapter (a cheap os-release read, cached on the adapter object —
    always current, never a stale capability row). Detection failure =
    plain source URL: honest and slow beats binary 404s.

    Site policy ppm_binaries:false forces plain source everywhere
    (aba2's ABI posture: PPM linux binaries target the distro's system
    R + libs, weft's R is conda's — the load-check + source-rebuild
    arm compensates, but a pack owner may prefer never mixing; every
    probe gets its override). Stamped on the adapter at construction."""
    if getattr(adapter, "_weft_ppm_binaries", True) is False:
        return url
    if "packagemanager.posit.co" not in url:
        return url
    cached = getattr(adapter, "_weft_os_release", None)
    if cached is None:
        try:
            r = adapter.run_cmd(
                '. /etc/os-release 2>/dev/null; '
                'echo "${ID:-none}:${VERSION_CODENAME:-none}"', timeout=30)
            parts = (r.out or "").strip().split(":")
            cached = (parts[0], parts[1]) if len(parts) == 2 \
                else ("none", "none")
        except WeftError:
            cached = ("none", "none")
        adapter._weft_os_release = cached
    return ppm_platform_url(url, cached[0], cached[1])


def _build_jobs_prefix(build_jobs: int | None) -> str:
    """Shell prefix exporting the source-build parallelism pair: R reads
    WEFT_BUILD_JOBS into options(Ncpus) (across packages), make reads
    MAKEFLAGS (inside one). Uncapped nproc would be impolite on shared
    login nodes — the cap is min(nproc, build_jobs or 8), site policy
    max_build_cores being the lever behind build_jobs."""
    cap = int(build_jobs) if build_jobs else 8
    return (f'WEFT_NCPU=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2); '
            f'WEFT_BUILD_JOBS=$((WEFT_NCPU<{cap}?WEFT_NCPU:{cap})); '
            f'export WEFT_BUILD_JOBS; '
            f'export MAKEFLAGS="-j$WEFT_BUILD_JOBS"; ')


class _rlib_progress:
    """Poll the library dir while a long install runs and report the
    frontier (each completed package = one subdir): the measured trace
    was 11.7 SILENT minutes and drew two user cancels. Read-only `ls`
    observation every 5s on its own thread; never touches the install."""

    def __init__(self, adapter, rlib: str, total: int, progress):
        self.adapter, self.rlib = adapter, rlib
        self.total, self.progress = total, progress
        self._stop = None

    def __enter__(self):
        if self.progress is None or self.total <= 0:
            return self
        import shlex
        import threading

        self._stop = threading.Event()

        def _poll():
            last = -1
            while not self._stop.wait(5.0):
                try:
                    r = self.adapter.run_cmd(
                        f"ls {shlex.quote(self.rlib)} 2>/dev/null | "
                        f"grep -v '^00LOCK' | wc -l", timeout=30)
                    done = int((r.out or "0").strip() or 0)
                except (WeftError, ValueError):
                    continue        # observation only: never fail the install
                if done != last:
                    last = done
                    try:
                        self.progress(done=min(done, self.total),
                                      total=self.total)
                    except Exception:
                        pass
        threading.Thread(target=_poll, daemon=True).start()
        return self

    def __exit__(self, *exc):
        if self._stop is not None:
            self._stop.set()
        return False


def _conda_delta(records: list[dict], conda_packages: dict | None,
                 top_names: set) -> tuple[list[dict], list[dict]]:
    """Drop closure members the conda layer ALREADY provides (conda-forge
    spells CRAN's `Pkg` as `r-pkg`): aba 1.2 measured 25 of 26 cran
    installs re-building, from source, packages the conda layer had
    binary-installed a minute earlier. Only CLOSURE members are dropped —
    a top-level cran/github ask is an explicit request for the cran
    layer's version and always stays. The drop is flat, not graph-walked:
    if conda provides P, conda's own closure provided P's deps too, so
    they fall to the same rule; a dep reachable only through a KEPT
    record is never conda-satisfied-by-construction and stays. Returns
    (kept_records_with_pruned_dep_edges, substitution_facts) — the facts
    enter the lock (they ARE part of the solve's answer) with both
    versions, so a runtime too-old-dep failure is diagnosable."""
    if not conda_packages:
        return records, []
    kept, satisfied = [], []
    for r in records:
        conda_name = "r-" + r["name"].lower()
        if r["name"] not in top_names and not r.get("remote_sha") \
                and conda_name in conda_packages:
            satisfied.append({"name": r["name"], "conda": conda_name,
                              "conda_version": conda_packages[conda_name],
                              "snapshot_version": r["version"]})
            continue
        kept.append(r)
    if satisfied:
        kept_names = {r["name"] for r in kept}
        for r in kept:
            if r.get("deps"):
                r["deps"] = [d for d in r["deps"] if d in kept_names]
    satisfied.sort(key=lambda s: s["name"])
    return kept, satisfied


class PixiSolver:
    """The conda(+pypi) layer — wraps lock.solve; realization is the
    base strategy's job (prefix/packed), so realize_layer is a no-op."""

    ecosystem = "conda"
    conda_requirements: tuple[str, ...] = ()

    def __init__(self, pixi_bin: str):
        self.pixi_bin = pixi_bin

    def solve(self, deps, spec, workdir, **_):  # handled by lock.solve upstream
        raise NotImplementedError("pixi layer is solved via lock.solve")

    def realize_layer(self, layer, adapter, env_rel, **_):
        return ""

    def why(self, env_row: dict, package: str, workdir: Path) -> str:
        import subprocess
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "pixi.toml").write_text(env_row["manifest"])
        (workdir / "pixi.lock").write_text(env_row["native_lock"])
        r = subprocess.run(
            [self.pixi_bin, "tree", "--manifest-path",
             str(workdir / "pixi.toml"), "--invert", package],
            capture_output=True, text=True, timeout=300,
        )
        out = (r.stdout or r.stderr).strip()
        return out[-3000:] if out else f"{package}: not found in the conda layer"


# Release-pinned repository providers: given a release id, return the
# repository URLs that release freezes (possibly several companion repos
# under one version) and the runtime it requires. A release IS a snapshot
# — it pins identity exactly like a dated mirror. Hosts register their
# own; the registry mirrors the solver/fetcher/transfer pattern.
#   fn(release: str) -> {"repos": [url, ...], "r_version": "4.4" | None}
RELEASE_REPO_PROVIDERS: dict[str, object] = {}


def register_release_repo_provider(name: str, fn) -> None:
    RELEASE_REPO_PROVIDERS[name] = fn


class CranSolver:
    """CRAN + GitHub R dependencies, without pak.

    Resolution is base-R metadata against a **dated Posit Package Manager
    snapshot** (frozen forever — the date is the reproducibility anchor,
    recorded in the layer): `available.packages()` for versions,
    `tools::package_dependencies(recursive=TRUE)` for the closure.
    GitHub refs (`owner/repo@ref`) resolve to exact commit SHAs via the
    GitHub API, with DESCRIPTION parsed for their Imports/Depends.

    Realization: `install.packages()` against the same snapshot (PPM
    serves Linux *binaries* through the source API when the UserAgent
    carries the R version — fast), then GitHub tarballs by SHA. Needs
    network at the install point in v1; the failure hint says so.
    """

    ecosystem = "cran"
    conda_requirements = ("r-base",)
    # PLAIN snapshot in the lock (platform-neutral identity); the
    # __linux__/<codename> binary segment is applied at REALIZE time per
    # site via ppm_platform_url — the old constant baked focal into every
    # lock and served source tarballs to every non-focal host
    PPM = "https://packagemanager.posit.co/cran/{date}"

    def __init__(self, pixi_bin: str, home: Path | None = None):
        import os
        self.pixi_bin = pixi_bin
        self.home = Path(home or os.environ.get(
            "WEFT_SOLVER_HOME",
            Path.home() / ".cache" / "weft" / "solverenvs")) / "cran"

    def _ensure_solver_env(self) -> Path:
        import subprocess
        manifest = self.home / "pixi.toml"
        marker = self.home / ".ready"
        if marker.exists():
            return manifest
        self.home.mkdir(parents=True, exist_ok=True)
        from .spec import current_platform
        manifest.write_text(
            '[workspace]\nname = "weft-cran-solver"\n'
            f'channels = ["conda-forge"]\nplatforms = ["{current_platform()}"]\n\n'
            '[dependencies]\nr-base = "*"\n'
        )
        r = subprocess.run(
            [self.pixi_bin, "install", "--manifest-path", str(manifest)],
            capture_output=True, text=True, timeout=1800,
        )
        if r.returncode != 0:
            raise WeftError(
                "env.solve_failed",
                "could not build the controller-side R solver env",
                stage="solve", retryable=True,
                hints={"ecosystem": "cran",
                       "solver_message": (r.stderr or r.stdout)[-1000:]},
            )
        marker.write_text("ok\n")
        return manifest

    def _rscript(self, code: str, timeout: float = 900):
        import os
        import subprocess
        manifest = self._ensure_solver_env()
        # C locale: solve-failure classification keys on R's message text
        # ("unable to access index...") — translated messages would dodge it
        env = {**os.environ, "LC_ALL": "C", "LANGUAGE": "C"}
        return subprocess.run(
            [self.pixi_bin, "run", "--manifest-path", str(manifest),
             "Rscript", "-e", code],
            capture_output=True, text=True, timeout=timeout, env=env,
        )

    # -- ref parsing -----------------------------------------------------------

    @staticmethod
    def _parse(dep: str) -> dict:
        # ONE grammar for the whole vocabulary — spec.parse_cran_dep.
        # This lane's private parser split on the FIRST '/', turned
        # owner/repo/subdir@ref into a three-segment "repo", and
        # reported the resulting 404 as an unresolvable repository
        # (2026-07 field note: a live public repo declared missing).
        from .spec import parse_cran_dep
        return parse_cran_dep(dep)

    @staticmethod
    def _github_resolve(repo: str, ref: str, subdir: str | None = None) -> dict:
        import json as _json
        import urllib.request

        def get(url):
            req = urllib.request.Request(url, headers={"User-Agent": "weft"})
            return urllib.request.urlopen(req, timeout=30).read()

        import urllib.error

        def transient(e, what):
            return WeftError(
                "env.solve_failed",
                f"github {what} resolving {repo}@{ref}",
                stage="solve", retryable=True,
                hints={"ecosystem": "cran",
                       **({"http_status": e.code}
                          if isinstance(e, urllib.error.HTTPError) else {}),
                       "solver_message": str(e)[-300:],
                       "suggestion": "rate-limited, server-side, or "
                                     "network trouble from the "
                                     "controller; retry later"})

        _GONE = (404, 410, 422)
        # TWO different 404s, two different verdicts: the commits API
        # failing means the repo/ref is not there; DESCRIPTION failing
        # with a LIVE repo/ref means the package is not where we looked
        # — which, before subdir support, misdiagnosed every
        # subdirectory package as an unresolvable repository
        try:
            sha = _json.loads(
                get(f"https://api.github.com/repos/{repo}/commits/{ref}")
            )["sha"]
        except urllib.error.HTTPError as e:
            if e.code in _GONE:
                raise WeftError(
                    "env.solve_conflict",
                    f"cannot resolve github ref {repo}@{ref} — the "
                    f"repo or ref does not exist (or is private)",
                    stage="solve",
                    hints={"ecosystem": "cran",
                           "user_pins": [f"{repo}@{ref}"],
                           "http_status": e.code,
                           "solver_message": str(e)[-300:],
                           "suggestion": "check owner/repo spelling and "
                                         "that the ref exists"},
                ) from e
            raise transient(e, "api error") from e
        except Exception as e:
            raise transient(e, "unreachable") from e
        where = f"{subdir}/DESCRIPTION" if subdir else "DESCRIPTION"
        try:
            desc = get(f"https://raw.githubusercontent.com/{repo}/{sha}/"
                       f"{where}").decode()
        except urllib.error.HTTPError as e:
            if e.code in _GONE:
                raise WeftError(
                    "env.solve_conflict",
                    f"{repo}@{ref} exists, but there is no DESCRIPTION "
                    f"at {subdir or 'the repository root'}",
                    stage="solve",
                    hints={"ecosystem": "cran",
                           "user_pins": [f"{repo}@{ref}"],
                           "http_status": e.code,
                           "suggestion": "if the package lives in a "
                                         "subfolder, say "
                                         "owner/repo/subdir@ref"},
                ) from e
            raise transient(e, "api error") from e
        except Exception as e:
            raise transient(e, "unreachable") from e
        fields = {}
        key = None
        for line in desc.splitlines():
            if line[:1].isspace() and key:
                fields[key] += " " + line.strip()
            elif ":" in line:
                key, _, v = line.partition(":")
                fields[key.strip()] = v.strip()
        deps = []
        for f in ("Depends", "Imports", "LinkingTo"):
            for item in (fields.get(f) or "").split(","):
                nm = item.strip().split("(")[0].strip()
                if nm and nm != "R":
                    deps.append(nm)
        return {"name": fields.get("Package", repo.split("/")[-1]),
                "version": fields.get("Version", ""), "sha": sha,
                "repo": repo, "ref": ref, "subdir": subdir, "deps": deps}

    # -- Solver interface --------------------------------------------------------

    def _repo_set(self, spec) -> tuple[list[str], str, list[dict]]:
        """The full ordered repository set: the dated base snapshot, any
        extra CRAN-like repos the spec names, and every repo a release-
        pinned provider expands to. Resolved JOINTLY — a package in a
        secondary repo may depend on base-mirror packages and vice versa."""
        import datetime
        # default snapshot: UTC-today − 2, never the local calendar — a
        # controller ahead of UTC (or ahead of the mirror's publishing
        # lag) would ask for a snapshot that does not exist yet, failing
        # every unpinned solve in a nightly dead window (2026-07 note #4)
        date = (getattr(spec, "system_requirements", {}) or {}).get(
            "cran_snapshot") or (
            datetime.datetime.now(datetime.timezone.utc).date()
            - datetime.timedelta(days=2)).isoformat()
        snapshot = self.PPM.format(date=date)
        repos = [snapshot]
        for url in getattr(spec, "r_repositories", None) or []:
            if url not in repos:
                repos.append(url)
        releases = []
        for rr in getattr(spec, "r_release_repos", None) or []:
            provider = RELEASE_REPO_PROVIDERS.get(rr.get("provider", ""))
            if provider is None:
                raise WeftError(
                    "task.invalid",
                    f"no release-repo provider {rr.get('provider')!r} "
                    "registered", stage="solve",
                    hints={"registered": sorted(RELEASE_REPO_PROVIDERS),
                           "suggestion": "register one with weft.solvers."
                                         "register_release_repo_provider"})
            info = provider(rr["release"])
            releases.append({"provider": rr["provider"],
                             "release": rr["release"],
                             "repos": list(info["repos"]),
                             "r_version": info.get("r_version")})
            for url in info["repos"]:
                if url not in repos:
                    repos.append(url)
            self._check_release_runtime(rr, info, spec)
        return repos, snapshot, releases

    @staticmethod
    def _check_release_runtime(rr: dict, info: dict, spec) -> None:
        """A release line is tied to an interpreter version: reject a spec
        whose conda r-base cannot match, BEFORE any solving is paid for."""
        need = info.get("r_version")
        if not need:
            return
        import re
        from .spec import split_constraint
        constraint = None
        for dep in getattr(spec, "conda", []) or []:
            name, c = split_constraint(dep)
            if name == "r-base" and c not in ("*",):
                constraint = c
                break
        if constraint is None:
            return      # unpinned r-base: the joint solve may still pick
                        # a compatible one; nothing provable to reject here
        # refuse only a PROVABLE contradiction: an exact pin (=/== ) to a
        # different major.minor. Ranged constraints (>=4.3, <5) may still
        # admit the release's R — an ad-hoc lstrip("=").split() here read
        # ">=4.4" as the version and refused specs that were fine
        # (parser-sweep find #2; same class as the whitespace-split bug)
        m = re.match(r"={1,2}\s*(\d+(?:\.\d+)*)", constraint)
        if m is None:
            return      # not an exact pin: nothing provable to reject
        have = m.group(1)
        if not (have == need or have.startswith(need + ".")):
            raise WeftError(
                "env.layer_conflict",
                f'release {rr["provider"]}/{rr["release"]} requires '
                f"r-base {need}.*, but the conda layer pins r-base {have}",
                stage="solve",
                hints={"release": rr, "requires_r": need, "have_r": have,
                       "suggestion": f'pin "r-base ={need}" in deps.conda, '
                                     "or pick the release line matching "
                                     "your R version"})

    def solve(self, deps: list[str], spec, workdir: Path,
              conda_packages: dict | None = None) -> dict:
        import json as _json
        workdir.mkdir(parents=True, exist_ok=True)
        parsed = [self._parse(d) for d in deps]
        gh = [self._github_resolve(p["repo"], p["ref"], p.get("subdir"))
              for p in parsed if p["kind"] == "github"]
        cran_direct = [p for p in parsed if p["kind"] == "cran"]
        want = [p["name"] for p in cran_direct] + \
               [d for g in gh for d in g["deps"]]
        # a dated snapshot is the reproducibility anchor: same date, same
        # answers, forever (extra repos and release lines pin the same way
        # — a release IS a snapshot of a coherent set).
        repos, snapshot, releases = self._repo_set(spec)
        out = workdir / "closure.tsv"
        code = (
            'options(repos=c({repovec}));'
            'ap <- available.packages();'
            # STATIC base list: `installed.packages(priority=...)` of the
            # solver's own R made the closure — the LOCK — depend on which
            # controller solved it (conda r-base has no "recommended"
            # packages; a distro R does)
            'base <- c({static_base});'
            'want <- setdiff(c({want}), c(base, ""));'
            'miss <- setdiff(want, rownames(ap));'
            'if (length(miss)) {{ write(paste("MISSING:", paste(miss, collapse=",")), stderr()); quit(status=3) }};'
            'cl <- unique(c(want, unlist(tools::package_dependencies(want, db=ap, recursive=TRUE))));'
            'cl <- setdiff(cl, base); cl <- intersect(cl, rownames(ap));'
            # per-package direct deps: the graph offline installs need for
            # topological ordering (packed layers, design-next B2)
            'dg <- tools::package_dependencies(cl, db=ap, recursive=FALSE);'
            'dgs <- vapply(cl, function(p) paste(setdiff(intersect(dg[[p]], cl), base), collapse=","), "");'
            # which repository actually serves each package (joint
            # resolution across the whole set — identity records reality)
            'rp <- sub("/src/contrib.*$", "", ap[cl, "Repository"]);'
            'write.table(data.frame(ap[cl, "Package"], ap[cl, "Version"], dgs, rp), {out}, sep="\\t", '
            'row.names=FALSE, col.names=FALSE, quote=FALSE)'
        ).format(repovec=", ".join(
                     f"R{i}={_json.dumps(u)}" for i, u in enumerate(repos)),
                 static_base=", ".join(_json.dumps(b)
                                       for b in BASE_R_PACKAGES),
                 want=", ".join(_json.dumps(x) for x in want) or '""',
                 out=_json.dumps(str(out)))
        if want:
            r = self._rscript(code)
            if r.returncode != 0:
                msg = (r.stderr or r.stdout)[-1200:]
                if "unable to access index for repository" in \
                        (r.stderr or "") + (r.stdout or ""):
                    # an unreachable index empties available.packages()
                    # SILENTLY — every wanted package then looks
                    # "missing" and the old verdict blamed the spec
                    # (2026-07 field note #3)
                    raise WeftError(
                        "env.solve_failed",
                        "an R repository index is unreachable from the "
                        "controller — the packages are not missing, the "
                        "index is",
                        stage="solve", retryable=True,
                        hints={"ecosystem": "cran", "repos": repos,
                               "solver_message": msg,
                               "suggestion": "network/proxy to the "
                                             "repository; retry, or point "
                                             "r_repositories at a "
                                             "reachable mirror"},
                    )
                raise WeftError(
                    "env.solve_conflict",
                    "cran layer is unsatisfiable against the repository set",
                    stage="solve",
                    hints={"ecosystem": "cran", "user_pins": deps,
                           "snapshot": snapshot, "repos": repos,
                           "solver_message": msg,
                           "suggestion": "package name typo, not in any "
                                         "configured repo (add it via "
                                         "r_repositories / r_release_repos) "
                                         "— or owner/repo@ref for github"},
                )
            rows = [l.split("\t") for l in out.read_text().splitlines() if l]
        else:
            rows = []
        records = [{"name": r[0], "version": r[1],
                    "source": (r[3] if len(r) > 3 and r[3] else snapshot),
                    "sha256": "",
                    "deps": [d for d in (r[2] if len(r) > 2 else ""
                                         ).split(",") if d]}
                   for r in rows]
        top_names = {p["name"] for p in cran_direct}
        records, satisfied = _conda_delta(records, conda_packages,
                                          top_names)
        for p in cran_direct:  # exact-version assertions
            if p["version"]:
                got = next((r["version"] for r in records
                            if r["name"] == p["name"]), None)
                if got != p["version"]:
                    raise WeftError(
                        "env.solve_conflict",
                        f'{p["name"]} =={p["version"]} not satisfiable: '
                        f"snapshot has {got}",
                        stage="solve",
                        hints={"ecosystem": "cran", "user_pins": deps,
                               "snapshot": snapshot,
                               "suggestion": "drop the pin (snapshot already "
                                             "freezes versions) or change it "
                                             f"to =={got}"},
                    )
        for g in gh:
            records.append({
                "name": g["name"], "version": g["version"],
                "source": f'github:{g["repo"]}'
                          + (f'/{g["subdir"]}' if g.get("subdir") else "")
                          + f'@{g["ref"]}',
                # subdir rides the record so every install site knows the
                # package is NOT at the tarball root (whole-repo tarball)
                **({"subdir": g["subdir"]} if g.get("subdir") else {}),
                "remote_sha": g["sha"], "sha256": "",
                "tarball": f'https://codeload.github.com/{g["repo"]}'
                           f'/tar.gz/{g["sha"]}',
            })
        records.sort(key=lambda x: (x["name"], x["version"]))
        gh_names = {g["name"] for g in gh}
        return {"records": records, "snapshot": snapshot,
                "repos": repos, "releases": releases,
                **({"satisfied_by_conda": satisfied} if satisfied else {}),
                # what an extends_env child must inherit to re-solve
                # against the SAME repository universe
                "spec_config": {
                    k: v for k, v in (
                        ("r_repositories",
                         list(getattr(spec, "r_repositories", None) or [])),
                        ("r_release_repos",
                         list(getattr(spec, "r_release_repos", None) or [])),
                    ) if v},
                "native": _json.dumps({"snapshot": snapshot, "repos": repos,
                                       "releases": releases,
                                       "records": records}, indent=1),
                "from_source": sorted(gh_names),
                "top_level": sorted({p["name"] for p in cran_direct}
                                    | gh_names)}

    @staticmethod
    def inherit_pins(layer: dict) -> tuple[list[str], dict[str, str]]:
        """Exact pins that reproduce this layer's top-level set in a child
        solve (extends_env): github packages pin to the resolved COMMIT SHA
        (a re-solved branch ref would move — and a bare name would silently
        become the same-versioned CRAN release), the rest pin exact versions;
        the snapshot date carries over so the transitive closure re-resolves
        identically."""
        import re
        by_name = {r["name"]: r for r in layer.get("records", [])}
        pins = []
        for name in layer.get("top_level", []):
            rec = by_name.get(name)
            if rec is None:
                continue
            src = rec.get("source", "")
            if src.startswith("github:") and rec.get("remote_sha"):
                repo = src[len("github:"):].split("@")[0]
                pins.append(f'{repo}@{rec["remote_sha"]}')
            else:
                pins.append(f'{name} =={rec["version"]}')
        sysreq = {}
        m = re.search(r"(\d{4}-\d{2}-\d{2})", layer.get("snapshot") or "")
        if m:
            sysreq["cran_snapshot"] = m.group(1)
        return pins, sysreq

    def realize_layer(self, layer: dict, adapter, env_rel: str,
                      progress=None, build_jobs: int | None = None) -> str:
        import json as _json
        import shlex
        env_dir = adapter.path(env_rel)
        rlib = f"{env_dir}/rlib"
        cran_names = [r["name"] for r in layer["records"]
                      if not r.get("remote_sha")]
        top = layer.get("top_level", [])
        repos = [site_ppm_url(u, adapter)
                 for u in (layer.get("repos") or [layer["snapshot"]])]
        rcode = (
            # Ncpus parallelizes ACROSS packages, MAKEFLAGS (set by the
            # shell wrapper) inside each build — a 226 s single-core
            # stringi on a many-core box was the measured cost of neither
            'options(Ncpus=max(1L, as.integer(Sys.getenv('
            '"WEFT_BUILD_JOBS", "2"))));'
            'options(repos=c({repovec}), HTTPUserAgent=sprintf('
            '"R/%s R (%s)", getRversion(), paste(getRversion(), '
            'R.version$platform, R.version$arch, R.version$os)));'
            'lib <- {lib}; dir.create(lib, showWarnings=FALSE, recursive=TRUE);'
            # skip what ANY activated library already provides (conda's
            # site-library rides .libPaths through activate.sh): pre-delta
            # locks re-installed 25/26 conda-satisfied packages here
            'have <- unlist(lapply(.libPaths(), function(l) '
            'rownames(installed.packages(lib.loc=l))));'
            'p <- c({pkgs}); p <- setdiff(p, c(have, ""));'
            'if (length(p)) install.packages(p, lib=lib);'
            # PPM linux binaries assume distro glibc; on older hosts they
            # install but fail to *load* — detect and rebuild those from source
            'chk <- intersect(c({pkgs}), rownames(installed.packages(lib.loc=lib)));'
            'bad <- Filter(function(x) inherits(tryCatch('
            'loadNamespace(x, lib.loc=lib), error=function(e) e), "error"), chk);'
            'if (length(bad)) {{'
            ' write(paste("binary load failed, rebuilding from source:",'
            ' paste(bad, collapse=",")), stderr());'
            ' srcrepo <- sub("__linux__/[^/]+/", "", getOption("repos"));'
            ' remove.packages(bad, lib=lib);'
            ' install.packages(bad, lib=lib, repos=srcrepo, type="source") }};'
            '{ghinstall}'
            'need <- c({top});'
            # presence across ALL activated libraries — a top-level ask
            # satisfied by the conda layer is present, not failed
            'have2 <- unlist(lapply(.libPaths(), function(l) '
            'rownames(installed.packages(lib.loc=l))));'
            'ok <- need %in% c(have2, rownames(installed.packages(lib.loc=lib)));'
            'if (!all(ok)) {{ write(paste("FAILED:", paste(need[!ok], collapse=",")), stderr()); quit(status=4) }}'
        ).format(repovec=", ".join(
                     f"R{i}={_json.dumps(u)}" for i, u in enumerate(repos)),
                 lib=_json.dumps(rlib),
                 pkgs=", ".join(_json.dumps(x) for x in cran_names) or '""',
                 ghinstall=_r_gh_install(layer["records"]),
                 top=", ".join(_json.dumps(x) for x in top) or 'character(0)')
        # converge, don't flinch: the R code is incremental (installed
        # packages are skipped, broken binaries re-detected), so when a
        # weird site kills the long install mid-flight (login-node walls
        # that cut ~45-min commands exist — clip taught us), rerunning
        # picks up the frontier. Retry while the library keeps CHANGING;
        # a real failure repeats with the library frozen and raises.
        total = len(cran_names) + sum(1 for r in layer["records"]
                                      if r.get("remote_sha"))
        # FULL output persists site-side (th594060f7 item 1: three blind
        # realizes because the 3-hour install's log lived and died in
        # controller memory; the causal lines sat outside every window)
        from .evidence import _syslib_hints, failure_evidence, run_logged
        log_rel = f"logs/{env_rel.rsplit('/', 1)[-1]}-cran-realize.log"
        last_state = None
        for _ in range(8):
            with _rlib_progress(adapter, rlib, total, progress):
                r = run_logged(
                    adapter,
                    f". {shlex.quote(env_dir)}/activate.sh && "
                    f"{_build_jobs_prefix(build_jobs)}"
                    f"Rscript -e {shlex.quote(rcode)}",
                    log_rel,
                    # on old-glibc hosts every PPM binary fails to load and
                    # the WHOLE layer rebuilds from source (rstan ~20 min)
                    timeout=10800,
                    runner=adapter.run_activated,
                )
            if r.rc == 0:
                return f'export R_LIBS="{rlib}' + '${R_LIBS:+:$R_LIBS}"'
            probe = adapter.run_cmd(
                f"ls {shlex.quote(rlib)} 2>/dev/null | sort | cksum")
            state = (probe.out or "").strip()
            if state == last_state:
                break                     # frozen library = real failure
            last_state = state
        raise WeftError(
            "env.realize_failed",
            "cran layer install failed on site",
            stage="realize",
            hints={"ecosystem": "cran",
                   **(ev := failure_evidence(adapter, log_rel,
                                             r.err or r.out)),
                   **(_syslib_hints((r.err or "") + (r.out or "")) or {}),
                   # gated on the evidence: the unconditional
                   # air-gapped text steered the r-signac agent toward
                   # networking on a dependency-NAME failure
                   "note": _remedies.cran_realize_note(
                       ev.get("error_regions"), r.out or "")},
        )

    def realize_overlay(self, layer: dict, parent_layer: dict | None,
                        added: list[str], adapter, env_rel: str,
                        parent_rel: str, prelude: str, pack_tools: dict,
                        parent_env_id: str) -> str:
        """Install ONLY the delta packages into this env's own rlib and put
        it *in front of* the parent's on R_LIBS. R composes library paths
        natively — this is the ecosystem doing the layering, not us.

        Source builds (GitHub refs, packages with C code) use the weft-owned
        toolchain from `prelude` and are cached content-addressed by
        (source, parent env, platform, toolchain)."""
        import json as _json
        import shlex
        from .toolchain import cached_build, compile_cache_key, put_cached_build

        env_dir = adapter.path(env_rel)
        parent_dir = adapter.path(parent_rel)
        # squashfs parents hold their tree (incl. rlib) inside the mount;
        # activation still goes through the OUTER parent_dir script
        layout_dir = pack_tools.get("parent_layout_dir") or parent_dir
        rlib = f"{env_dir}/rlib"
        parent_rlib = f"{layout_dir}/rlib"

        by_name = {r["name"]: r for r in layer["records"]}
        recs = [by_name[n] for n in added if n in by_name]
        store = pack_tools.get("store")
        cas = pack_tools.get("cas")
        transfers = pack_tools.get("transfers", {})

        # compile cache: has this exact package already been built against
        # this exact parent? then nobody pays twice — not the next workspace,
        # not the next colleague on a shared site.
        key = compile_cache_key(
            {"records": [{k: r.get(k) for k in ("name", "version", "source",
                                                "remote_sha")} for r in recs],
             "snapshot": layer.get("snapshot"),
             # what actually compiled and linked the artifact: the resolved
             # toolchain and the parent prefix embedded in its rpath
             "toolchain_lock": pack_tools.get("toolchain_fingerprint"),
             "parent_prefix": pack_tools.get("parent_prefix")},
            parent_env_id, pack_tools.get("site_platform") or "linux-64")
        hit = cached_build(store, key) if store is not None else None
        adapter.run_cmd(f"mkdir -p {shlex.quote(rlib)}")
        if hit and cas is not None:
            endpoint = adapter.transfer_endpoint()
            method = transfers.get(endpoint["method"])
            row = store.get_dataref(hit)
            digest = hit.split(":")[-1]
            method.transfer([(digest, row["bytes"])], cas, endpoint,
                            verify={digest: row["meta"].get("sha256_plain")
                                    or digest})
            blob = f"{endpoint['cas_root']}/{digest[:2]}/{digest}"
            r = adapter.run_cmd(
                f"tar -xf {shlex.quote(blob)} -C {shlex.quote(rlib)}",
                timeout=600)
            if r.rc == 0:
                store.emit("overlay.compile_cache_hit", key=key,
                           packages=[x["name"] for x in recs])
                return self._r_libs_line(rlib, parent_rlib)
            # a cached blob that will not extract is corrupt — falling
            # through to a fresh build is right, but doing it SILENTLY
            # hides a permanently poisoned cache entry (sweep #5: the
            # decode-corrupted tars looked exactly like this). Demote the
            # mapping too: cached_build returns the FIRST key match, so a
            # poisoned entry would shadow every good re-cache forever.
            store.emit("overlay.compile_cache_bad", key=key, rc=r.rc,
                       err_tail=(r.err or r.out)[-300:])
            store.update_dataref_meta(hit, {"compile_cache": None})

        cran_names = [r["name"] for r in recs if not r.get("remote_sha")]
        rcode = (
            'options(Ncpus=max(1L, as.integer(Sys.getenv('
            '"WEFT_BUILD_JOBS", "2"))));'
            'options(repos=c({repovec}), HTTPUserAgent=sprintf('
            '"R/%s R (%s)", getRversion(), paste(getRversion(), '
            'R.version$platform, R.version$arch, R.version$os)));'
            'lib <- {lib}; dir.create(lib, showWarnings=FALSE, recursive=TRUE);'
            '.libPaths(c(lib, {plib}, .libPaths()));'
            # the parent's conda site-library and rlib both ride
            # .libPaths here — never rebuild what the stack already has
            'have <- unlist(lapply(.libPaths(), function(l) '
            'rownames(installed.packages(lib.loc=l))));'
            'p <- setdiff(c({pkgs}), c(have, ""));'
            'if (length(p)) install.packages(p, lib=lib);'
            # PPM binary load-check + per-package source rebuild — the
            # SAME arm realize_layer carries (aba2 ABI note: distro-built
            # binaries under conda R fail at LOAD, not install; presence
            # checks ratified broken installs on this lane)
            'chk <- intersect(c({pkgs}), rownames(installed.packages(lib.loc=lib)));'
            'bad <- Filter(function(x) inherits(tryCatch('
            'loadNamespace(x, lib.loc=lib), error=function(e) e), "error"), chk);'
            'if (length(bad)) {{'
            ' write(paste("binary load failed, rebuilding from source:",'
            ' paste(bad, collapse=",")), stderr());'
            ' srcrepo <- sub("__linux__/[^/]+/", "", getOption("repos"));'
            ' remove.packages(bad, lib=lib);'
            ' install.packages(bad, lib=lib, repos=srcrepo, type="source") }};'
            '{ghinstall}'
            'need <- c({need});'
            'have2 <- unlist(lapply(.libPaths(), function(l) '
            'rownames(installed.packages(lib.loc=l))));'
            'ok <- need %in% c(have2, rownames(installed.packages(lib.loc=lib)));'
            'if (!all(ok)) {{ write(paste("FAILED:", paste(need[!ok], collapse=",")), stderr()); quit(status=4) }}'
        ).format(repovec=", ".join(
                     f"R{i}={_json.dumps(u)}" for i, u in enumerate(
                         [site_ppm_url(u, adapter) for u in
                          (layer.get("repos") or [layer["snapshot"]])])),
                 lib=_json.dumps(rlib), plib=_json.dumps(parent_rlib),
                 pkgs=", ".join(_json.dumps(x) for x in cran_names) or 'character(0)',
                 ghinstall=_r_gh_install(recs),
                 need=", ".join(_json.dumps(x) for x in added) or 'character(0)')
        _w = pack_tools.get("wrap_cmd") or (lambda s: s)
        with _rlib_progress(adapter, rlib, len(recs),
                            pack_tools.get("progress")):
            # activation FIRST, prelude AFTER (same catch as the session
            # lane): the shell-hook's baked PATH reset silently dropped
            # the toolchain bin from every compile on system-compiler-
            # less sites
            _pl = prelude.replace("\n", "; ").rstrip("; ")
            from .evidence import (_syslib_hints, failure_evidence,
                                   run_logged)
            log_rel = (f"logs/{env_rel.rsplit('/', 1)[-1]}"
                       f"-cran-overlay.log")
            r = run_logged(adapter, _w(
                f". {shlex.quote(parent_dir)}/activate.sh && "
                + (_pl + " && " if _pl else "")
                + f"{_build_jobs_prefix(pack_tools.get('build_jobs'))}"
                f"Rscript -e {shlex.quote(rcode)} 2>&1"),
                log_rel, timeout=3600, runner=adapter.run_activated)
        if r.rc != 0:
            raise WeftError(
                "env.realize_failed",
                "cran overlay layer install failed",
                stage="realize",
                hints={"ecosystem": "cran",
                       **(ev := failure_evidence(adapter, log_rel,
                                                 r.err or r.out)),
                       **(_syslib_hints((r.err or "") + (r.out or ""))
                          or {}),
                       "note": _remedies.cran_overlay_note(
                           ev.get("error_regions"))})
        # populate the compile cache for everyone who comes next
        if store is not None and cas is not None:
            import tempfile
            from pathlib import Path as _P
            tar_rel = f"{env_rel}/rlib-cache.tar"
            tarred = adapter.run_cmd(
                f"tar -cf {shlex.quote(adapter.path(tar_rel))} "
                f"-C {shlex.quote(rlib)} .", timeout=600)
            if tarred.rc == 0:
                # rc unchecked would cache a truncated tar (disk full,
                # partial write) under a hash of the GARBAGE — valid-
                # looking, poisoned forever (sweep tar-rc finding)
                try:
                    data = adapter.read_file(tar_rel)
                    with tempfile.TemporaryDirectory() as td:
                        p = _P(td) / "rlib.tar"
                        p.write_bytes(data)
                        ref = put_cached_build(store, cas, key, p)
                    store.emit("overlay.compile_cached", key=key, ref=ref,
                               packages=added)
                except WeftError:
                    pass      # caching is an optimization, never a failure
            adapter.run_cmd(f"rm -f {shlex.quote(adapter.path(tar_rel))}")
        return self._r_libs_line(rlib, parent_rlib)

    @staticmethod
    def _r_libs_line(rlib: str, parent_rlib: str) -> str:
        return (f'export R_LIBS="{rlib}:{parent_rlib}'
                + '${R_LIBS:+:$R_LIBS}"')

    def pack_layer(self, layer: dict, adapter, env_rel: str,
                   pack_tools: dict) -> str:
        """Air-gapped delivery (design B2): download the locked closure
        controller-side, ship it as one CAS blob through the data plane,
        install offline in dependency order. Symmetric to conda's `packed`."""
        import json as _json
        import shlex
        import subprocess
        import tarfile
        import tempfile
        import urllib.request

        cas = pack_tools.get("cas")
        transfers = pack_tools.get("transfers", {})
        if cas is None:
            raise WeftError(
                "env.realize_failed",
                "packed cran layer needs the controller CAS",
                stage="realize")
        records = layer["records"]
        order = _topo_order(records)
        with tempfile.TemporaryDirectory(prefix="weft-cranpack-") as td:
            tdp = Path(td)
            (tdp / "src").mkdir()
            files = []
            for rec in order:
                if rec.get("remote_sha"):        # github: tarball by SHA
                    url, fn = rec["tarball"], f"{rec['name']}.tar.gz"
                else:            # source tarball from the repo that SERVED
                                 # it (base snapshot or a secondary repo);
                                 # strip ANY binary segment, not a literal
                                 # focal (a jammy PPM url from r_repositories
                                 # survived the old .replace and 404'd —
                                 # platform-sweep find A4)
                    import re as _re
                    base = _re.sub(r"__linux__/[^/]+/", "",
                                   rec.get("source") or layer["snapshot"])
                    url = (f"{base}/src/contrib/"
                           f"{rec['name']}_{rec['version']}.tar.gz")
                    fn = f"{rec['name']}_{rec['version']}.tar.gz"
                req = urllib.request.Request(url, headers={"User-Agent": "weft"})
                try:
                    data = urllib.request.urlopen(req, timeout=120).read()
                except Exception as e:
                    raise WeftError(
                        "env.realize_failed",
                        f"could not download {rec['name']} for offline packing",
                        stage="realize",
                        hints={"url": url, "detail": str(e)[-200:]}) from e
                if rec.get("remote_sha") and rec.get("subdir"):
                    # whole-repo tarball, package in a subfolder: re-pack
                    # controller-side so the site's plain `R CMD INSTALL
                    # file` loop stays unchanged
                    data = _subdir_tarball(data, rec["subdir"], rec["name"])
                (tdp / "src" / fn).write_bytes(data)
                files.append(fn)
            # one filename per line, in install order — a shell loop reads it
            (tdp / "order.txt").write_text("\n".join(files) + "\n")
            archive = tdp / "cran-layer.tar"
            with tarfile.open(archive, "w") as tar:
                tar.add(tdp / "src", arcname="src")
                tar.add(tdp / "order.txt", arcname="order.txt")
            info = cas.register_file(archive)

        digest = info.ref.split(":")[-1]
        endpoint = adapter.transfer_endpoint()
        method = transfers.get(endpoint["method"])
        method.transfer([(digest, info.bytes)], cas, endpoint,
                        verify={digest: info.plain_sha256 or digest})
        site_tar = f"{endpoint['cas_root']}/{digest[:2]}/{digest}"
        env_dir = adapter.path(env_rel)
        rlib = f"{env_dir}/rlib"
        pack_dir = f"{env_dir}/cran-pack"
        r = adapter.run_activated(
            f". {shlex.quote(env_dir)}/activate.sh && "
            f"rm -rf {shlex.quote(pack_dir)} && mkdir -p {shlex.quote(pack_dir)} "
            f"{shlex.quote(rlib)} && tar -xf {shlex.quote(site_tar)} "
            f"-C {shlex.quote(pack_dir)} && "
            # install in dependency order, offline, from source
            f"cd {shlex.quote(pack_dir)}/src && "
            f"while read -r f; do "
            f"R CMD INSTALL --library={shlex.quote(rlib)} \"$f\" || exit 1; "
            f"done < ../order.txt",
            timeout=7200)
        if r.rc != 0:
            raise WeftError(
                "env.realize_failed",
                "offline cran layer install failed on site",
                stage="realize",
                hints={"ecosystem": "cran",
                       "log_tail": (r.err or r.out)[-1500:],
                       # evidence wiring for this offline lane is task
                       # #124; the note GATE lands now (in-memory text)
                       "note": _remedies.packed_cran_note(
                           _extract_regions((r.err or "")
                                            + (r.out or "")))})
        return f'export R_LIBS="{rlib}' + '${R_LIBS:+:$R_LIBS}"'

    def why(self, env_row: dict, package: str, workdir: Path) -> str:
        return f"{package}: see the cran layer record (env_why returns it)"


def _r_gh_install(records: list[dict]) -> str:
    """R lines installing github tarballs, subdir-aware. The tarball is
    the WHOLE repo by SHA: install.packages on it builds the repo ROOT,
    so a subdir package must be untarred and built from its directory
    (2026-07 vocabulary round). Returns a fully-formed snippet — callers
    splice it into their rcode template as a pre-formatted argument."""
    import json as _json
    gh = [r for r in records if r.get("remote_sha")]
    if not gh:
        return ""
    t = ", ".join(_json.dumps(r["tarball"]) for r in gh)
    s = ", ".join(_json.dumps(r.get("subdir") or "") for r in gh)
    return (
        f't <- c({t}); s <- c({s});'
        'for (i in seq_along(t)) {'
        ' if (nzchar(s[i])) {'
        '  tf <- tempfile(fileext=".tar.gz");'
        '  download.file(t[i], tf, quiet=TRUE);'
        '  xd <- tempfile(); untar(tf, exdir=xd);'
        '  pd <- file.path(list.dirs(xd, recursive=FALSE)[1], s[i]);'
        '  install.packages(pd, lib=lib, repos=NULL, type="source")'
        ' } else install.packages(t[i], lib=lib, repos=NULL,'
        ' type="source")'
        '};'
    )


def _subdir_tarball(data: bytes, subdir: str, name: str) -> bytes:
    """Re-pack a whole-repo tarball down to just the subdir package
    (rooted at the package name) so the offline packed lane's plain
    `R CMD INSTALL file` keeps working unchanged."""
    import io
    import tarfile
    src = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    members = src.getmembers()
    top = members[0].name.split("/")[0]
    prefix = f"{top}/{subdir}/"
    out = io.BytesIO()
    kept = 0
    with tarfile.open(fileobj=out, mode="w:gz") as dst:
        for m in members:
            if not m.name.startswith(prefix):
                continue
            m.name = name + "/" + m.name[len(prefix):]
            dst.addfile(m, src.extractfile(m) if m.isfile() else None)
            kept += 1
    if not kept:
        raise WeftError(
            "env.realize_failed",
            f"the {name} tarball has no {subdir!r} directory — resolve "
            f"and archive disagree about the repo layout",
            stage="realize", hints={"subdir": subdir})
    return out.getvalue()


def _topo_order(records: list[dict]) -> list[dict]:
    """Dependency order for offline installs (the graph the solver stored)."""
    by_name = {r["name"]: r for r in records}
    out, seen, temp = [], set(), set()

    def visit(name: str) -> None:
        if name in seen or name not in by_name:
            return
        if name in temp:      # cycles shouldn't exist in CRAN; be safe
            return
        temp.add(name)
        for dep in by_name[name].get("deps", []):
            visit(dep)
        temp.discard(name)
        seen.add(name)
        out.append(by_name[name])

    for r in records:
        visit(r["name"])
    return out


def _julia_solve_error(msg: str, deps: list) -> WeftError:
    """Pkg.add does network work (registry update, git clones) — only
    its resolver's own verdict is a CONFLICT; everything else branded
    "unsatisfiable" fed envman's soft-pin relaxation a mislabeled
    conflict (2026-07 sweep A1). Marker is Pkg's documented resolver
    prefix (no julia conda build for this controller's platform to
    probe — recorded in tool_honesty.md)."""
    if "Unsatisfiable requirements detected" in msg:
        return WeftError(
            "env.solve_conflict",
            "julia layer is unsatisfiable as pinned",
            stage="solve",
            hints={"ecosystem": "julia", "user_pins": deps,
                   "solver_message": msg})
    return WeftError(
        "env.solve_failed",
        "julia solve failed before a resolver verdict (registry/network/"
        "git or a missing Manifest) — the pins are not implicated",
        stage="solve", retryable=True,
        hints={"ecosystem": "julia", "solver_message": msg})


class JuliaSolver:
    """Julia dependencies via Pkg — the easy ecosystem: Manifest.toml IS
    a content-addressed lockfile (git-tree-sha1 per package). Solving runs
    Pkg.add in a throwaway project on the controller (downloads go to a
    weft-owned depot, like pixi's cache); realization ships
    Project+Manifest and runs Pkg.instantiate against a shared per-site
    depot. Refs: "DataFrames", "DataFrames ==1.6.1", "owner/Repo.jl@ref".
    """

    ecosystem = "julia"
    conda_requirements = ("julia",)

    def __init__(self, pixi_bin: str, home: Path | None = None):
        import os
        self.pixi_bin = pixi_bin
        self.home = Path(home or os.environ.get(
            "WEFT_SOLVER_HOME",
            Path.home() / ".cache" / "weft" / "solverenvs")) / "julia"

    def _ensure_solver_env(self) -> Path:
        import subprocess
        manifest = self.home / "pixi.toml"
        if (self.home / ".ready").exists():
            return manifest
        self.home.mkdir(parents=True, exist_ok=True)
        from .spec import current_platform
        manifest.write_text(
            '[workspace]\nname = "weft-julia-solver"\n'
            f'channels = ["conda-forge"]\nplatforms = ["{current_platform()}"]\n\n'
            '[dependencies]\njulia = "*"\n')
        r = subprocess.run(
            [self.pixi_bin, "install", "--manifest-path", str(manifest)],
            capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise WeftError(
                "env.solve_failed",
                "could not build the controller-side julia solver env",
                stage="solve", retryable=True,
                hints={"ecosystem": "julia",
                       "solver_message": (r.stderr or r.stdout)[-1000:]})
        (self.home / ".ready").write_text("ok\n")
        return manifest

    @staticmethod
    def _add_expr(dep: str) -> str:
        import json as _json
        dep = dep.strip()
        if "/" in dep:                     # owner/Repo.jl[@ref]
            repo, _, ref = dep.partition("@")
            url = _json.dumps(f"https://github.com/{repo}")
            return (f"Pkg.add(url={url}, rev={_json.dumps(ref)})" if ref
                    else f"Pkg.add(url={url})")
        parts = dep.split()
        if len(parts) == 1:
            return f"Pkg.add({_json.dumps(parts[0])})"
        name, constraint = parts[0], " ".join(parts[1:])
        if constraint.startswith("=="):
            return (f"Pkg.add(name={_json.dumps(name)}, "
                    f"version={_json.dumps(constraint[2:].strip())})")
        raise WeftError(
            "task.invalid", f"julia constraint {dep!r} not supported",
            stage="solve",
            hints={"supported": ["Name", "Name ==X.Y.Z", "owner/Repo.jl@ref"]})

    def solve(self, deps: list[str], spec, workdir: Path,
              conda_packages: dict | None = None) -> dict:
        import subprocess
        workdir.mkdir(parents=True, exist_ok=True)
        manifest = self._ensure_solver_env()
        adds = "; ".join(self._add_expr(d) for d in deps)
        depot = self.home / "depot"
        r = subprocess.run(
            [self.pixi_bin, "run", "--manifest-path", str(manifest),
             "julia", "-e",
             f'using Pkg; Pkg.activate("{workdir}"); {adds}'],
            capture_output=True, text=True, timeout=1800,
            env={**__import__("os").environ,
                 "JULIA_DEPOT_PATH": str(depot)})
        if r.returncode != 0 or not (workdir / "Manifest.toml").exists():
            raise _julia_solve_error((r.stderr or r.stdout)[-1200:], deps)
        import tomllib
        man = tomllib.loads((workdir / "Manifest.toml").read_text())
        records = []
        for name, entries in (man.get("deps") or {}).items():
            e = entries[0] if isinstance(entries, list) else entries
            records.append({"name": name,
                            "version": e.get("version", ""),
                            "source": e.get("repo-url", "registry"),
                            "sha256": "",
                            "tree_sha1": e.get("git-tree-sha1", "")})
        records.sort(key=lambda x: (x["name"], x["version"]))
        return {"records": records,
                "native": ((workdir / "Project.toml").read_text()
                           + "\n###WEFT-MANIFEST###\n"
                           + (workdir / "Manifest.toml").read_text()),
                "from_source": [], "top_level": deps}

    def pack_layer(self, layer: dict, adapter, env_rel: str,
                   pack_tools: dict) -> str:
        """Air-gapped delivery (the same seam as CRAN's, design B2): the
        CONTROLLER instantiates the locked Manifest into a throwaway depot
        (packages + build artifacts land there), ships the depot subset as
        one CAS blob, and the site instantiates OFFLINE against it."""
        import shlex
        import subprocess
        import tarfile
        import tempfile

        cas = pack_tools.get("cas")
        transfers = pack_tools.get("transfers", {})
        if cas is None:
            raise WeftError(
                "env.realize_failed",
                "packed julia layer needs the controller CAS",
                stage="realize")
        manifest = self._ensure_solver_env()
        proj, _, man = layer["native"].partition("\n###WEFT-MANIFEST###\n")
        with tempfile.TemporaryDirectory(prefix="weft-juliapack-") as td:
            tdp = Path(td)
            (tdp / "proj").mkdir()
            (tdp / "proj" / "Project.toml").write_text(proj)
            (tdp / "proj" / "Manifest.toml").write_text(man)
            depot = tdp / "depot"
            r = subprocess.run(
                [self.pixi_bin, "run", "--manifest-path", str(manifest),
                 "julia", f"--project={tdp / 'proj'}", "-e",
                 "using Pkg; Pkg.instantiate()"],
                capture_output=True, text=True, timeout=3600,
                env={**__import__("os").environ,
                     "JULIA_DEPOT_PATH": str(depot)})
            if r.returncode != 0:
                raise WeftError(
                    "env.realize_failed",
                    "controller-side julia depot build failed",
                    stage="realize",
                    hints={"ecosystem": "julia",
                           "log_tail": (r.stderr or r.stdout)[-1200:]})
            archive = tdp / "julia-layer.tar"
            with tarfile.open(archive, "w") as tar:
                for sub in ("packages", "artifacts", "compiled"):
                    if (depot / sub).exists():
                        tar.add(depot / sub, arcname=sub)
            info = cas.register_file(archive)

        digest = info.ref.split(":")[-1]
        endpoint = adapter.transfer_endpoint()
        method = transfers.get(endpoint["method"])
        method.transfer([(digest, info.bytes)], cas, endpoint,
                        verify={digest: info.plain_sha256 or digest})
        site_tar = f"{endpoint['cas_root']}/{digest[:2]}/{digest}"
        env_dir = adapter.path(env_rel)
        adapter.write_file(f"{env_rel}/julia/Project.toml", proj.encode())
        adapter.write_file(f"{env_rel}/julia/Manifest.toml", man.encode())
        depot_site = adapter.path("cache/julia-depot")
        r = adapter.run_activated(
            f". {shlex.quote(env_dir)}/activate.sh && "
            f"mkdir -p {shlex.quote(depot_site)} && "
            f"tar -xf {shlex.quote(site_tar)} -C {shlex.quote(depot_site)} "
            f"&& JULIA_DEPOT_PATH={shlex.quote(depot_site)} "
            f"JULIA_PKG_OFFLINE=true "
            f"julia --project={shlex.quote(env_dir + '/julia')} "
            f"-e 'using Pkg; Pkg.instantiate()'",
            timeout=3600)
        if r.rc != 0:
            raise WeftError(
                "env.realize_failed",
                "offline julia instantiate failed on site", stage="realize",
                hints={"ecosystem": "julia",
                       "log_tail": (r.err or r.out)[-1500:],
                       "note": "the shipped depot covers packages/artifacts "
                               "the controller resolved; a platform mismatch "
                               "between controller and site can invalidate "
                               "BinaryBuilder artifacts"})
        return (f'export JULIA_PROJECT="{env_dir}/julia"\n'
                f'export JULIA_DEPOT_PATH="{depot_site}"\n'
                'export JULIA_PKG_OFFLINE=true')

    @staticmethod
    def inherit_pins(layer: dict) -> tuple[list[str], dict[str, str]]:
        """Exact pins for a child solve: registry packages pin to the
        resolved version; git refs are kept verbatim (their identity lives
        in the Manifest's tree-sha — a moved branch will surface as base
        drift in classify_delta, not silently)."""
        by_name = {r["name"]: r for r in layer.get("records", [])}
        pins = []
        for dep in layer.get("top_level", []):
            if "/" in dep:
                pins.append(dep)
                continue
            from .spec import strip_soft
            name = strip_soft(dep).split()[0].split("=")[0].strip()
            rec = by_name.get(name)
            if rec and rec.get("version"):
                pins.append(f'{name} =={rec["version"]}')
            else:
                pins.append(dep)
        return pins, {}

    def realize_layer(self, layer: dict, adapter, env_rel: str,
                      **_) -> str:
        import shlex
        env_dir = adapter.path(env_rel)
        proj, _, man = layer["native"].partition("\n###WEFT-MANIFEST###\n")
        adapter.write_file(f"{env_rel}/julia/Project.toml", proj.encode())
        adapter.write_file(f"{env_rel}/julia/Manifest.toml", man.encode())
        depot = adapter.path("cache/julia-depot")
        from .evidence import failure_evidence, run_logged
        log_rel = f"logs/{env_rel.rsplit('/', 1)[-1]}-julia-realize.log"
        r = run_logged(
            adapter,
            f". {shlex.quote(env_dir)}/activate.sh && "
            f"JULIA_DEPOT_PATH={shlex.quote(depot)} "
            f"julia --project={shlex.quote(env_dir + '/julia')} "
            f"-e 'using Pkg; Pkg.instantiate()'",
            log_rel, timeout=3600, runner=adapter.run_activated)
        if r.rc != 0:
            raise WeftError(
                "env.realize_failed", "julia layer instantiate failed on site",
                stage="realize",
                hints={"ecosystem": "julia",
                       **failure_evidence(adapter, log_rel,
                                          r.err or r.out),
                       "note": _remedies.julia_realize_note(
                           (r.err or "") + (r.out or ""))})
        return (f'export JULIA_PROJECT="{env_dir}/julia"\n'
                f'export JULIA_DEPOT_PATH="{depot}"')

    def realize_overlay(self, layer: dict, parent_layer: dict | None,
                        added: list[str], adapter, env_rel: str,
                        parent_rel: str, prelude: str, pack_tools: dict,
                        parent_env_id: str) -> str:
        """Julia layers on itself: the shared per-site depot already holds
        the parent's packages and JULIA_PROJECT is per-env by design — the
        overlay is just the child's Project/Manifest instantiated against
        the same depot (only the delta downloads). No compile cache needed:
        the depot IS one, keyed by git-tree-sha1."""
        import shlex
        env_dir = adapter.path(env_rel)
        parent_dir = adapter.path(parent_rel)
        proj, _, man = layer["native"].partition("\n###WEFT-MANIFEST###\n")
        adapter.write_file(f"{env_rel}/julia/Project.toml", proj.encode())
        adapter.write_file(f"{env_rel}/julia/Manifest.toml", man.encode())
        # depot STACK: ours first (writes land here), then the parent
        # root's (read-only base envs bring their packages along — Julia
        # only ever writes to the first entry)
        depot = adapter.path("cache/julia-depot")
        if "/envs/" in parent_rel:
            parent_depot = (parent_rel.rsplit("/envs/", 1)[0]
                            + "/cache/julia-depot")
            if parent_depot != depot and adapter.file_exists(parent_depot):
                depot = f"{depot}:{parent_depot}"
        _w = pack_tools.get("wrap_cmd") or (lambda s: s)
        from .evidence import failure_evidence, run_logged
        log_rel = f"logs/{env_rel.rsplit('/', 1)[-1]}-julia-overlay.log"
        r = run_logged(adapter, _w(
            f". {shlex.quote(parent_dir)}/activate.sh && "
            f"JULIA_DEPOT_PATH={shlex.quote(depot)} "
            f"julia --project={shlex.quote(env_dir + '/julia')} "
            f"-e 'using Pkg; Pkg.instantiate()'"),
            log_rel, timeout=3600, runner=adapter.run_activated)
        if r.rc != 0:
            raise WeftError(
                "env.realize_failed",
                "julia overlay instantiate failed on site", stage="realize",
                hints={"ecosystem": "julia",
                       **failure_evidence(adapter, log_rel,
                                          r.err or r.out)})
        return (f'export JULIA_PROJECT="{env_dir}/julia"\n'
                f'export JULIA_DEPOT_PATH="{depot}"')

    def why(self, env_row: dict, package: str, workdir: Path) -> str:
        return f"{package}: see the julia layer record (env_why returns it)"


def default_solvers(pixi_bin: str) -> dict[str, object]:
    """The registry. Adding an ecosystem = one class + one entry here
    (or inject externally via Weft(solvers={...}))."""
    return {
        "conda": PixiSolver(pixi_bin),
        "cran": CranSolver(pixi_bin),
        "julia": JuliaSolver(pixi_bin),
    }


def check_layer_requirements(spec, layers_present: dict[str, list[str]],
                             solvers: dict[str, object]) -> None:
    """Generic cross-layer contract: each solver declares what it needs
    from the conda layer; checked before any solving is paid for."""
    from .spec import split_constraint
    # ONE parser for the constraint grammar: an ad-hoc whitespace split
    # here refused "r-base=4.4" (the '=' pin never matched r-base) while
    # the hint itself told users to version-pin interpreters — aba 1.2,
    # live; the model dropped its pin to appease the check
    # union over shared deps AND per-platform variants: the extends_env
    # pins live in variants now (per-platform build strings), and an
    # inherited r-base must stay visible to the cran layer's check
    conda_names = {split_constraint(d)[0].lower() for d in spec.conda} | {
        split_constraint(d)[0].lower()
        for v in (spec.variants or {}).values()
        for d in (v.get("conda") or [])}
    for eco, deps in layers_present.items():
        if not deps:
            continue
        solver = solvers.get(eco)
        needs = getattr(solver, "conda_requirements", ()) if solver else ()
        missing = [p for p in needs if p.lower() not in conda_names]
        if missing:
            raise WeftError(
                "env.layer_conflict",
                f"{eco} layer requires {', '.join(missing)} from the conda layer",
                stage="solve",
                hints={"layer": eco, "needs": f"{missing[0]} in deps.conda",
                       "missing": missing,
                       "have_conda": sorted(conda_names),
                       "suggestion": f"add {missing} to deps.conda "
                                     "(version-pin interpreters: the layer's "
                                     "packages install against them)"},
            )
