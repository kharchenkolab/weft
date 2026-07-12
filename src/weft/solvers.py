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


class Solver(Protocol):
    ecosystem: str
    # conda packages this layer needs present in the base layer (the
    # cross-layer contract, enforced generically before any solve)
    conda_requirements: tuple[str, ...]

    def solve(self, deps: list[str], spec, workdir: Path) -> dict:
        """-> layer dict: {"records": [...sorted, hashed...],
        "native": <native lockfile text>, "requires": {...}}"""
        ...

    def realize_layer(self, layer: dict, adapter, env_rel: str) -> str:
        """Install the layer into the realized env dir on the site;
        return activation lines to append (e.g. R_LIBS exports)."""
        ...

    def why(self, env_row: dict, package: str, workdir: Path) -> str:
        """Reverse-dependency explanation for a package in this layer."""
        ...


class PixiSolver:
    """The conda(+pypi) layer — wraps lock.solve; realization is the
    base strategy's job (prefix/packed), so realize_layer is a no-op."""

    ecosystem = "conda"
    conda_requirements: tuple[str, ...] = ()

    def __init__(self, pixi_bin: str):
        self.pixi_bin = pixi_bin

    def solve(self, deps, spec, workdir):  # handled by lock.solve upstream
        raise NotImplementedError("pixi layer is solved via lock.solve")

    def realize_layer(self, layer, adapter, env_rel):
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
    PPM = "https://packagemanager.posit.co/cran/__linux__/focal/{date}"

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
        manifest.write_text(
            '[workspace]\nname = "weft-cran-solver"\n'
            'channels = ["conda-forge"]\nplatforms = ["linux-64"]\n\n'
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
        import subprocess
        manifest = self._ensure_solver_env()
        return subprocess.run(
            [self.pixi_bin, "run", "--manifest-path", str(manifest),
             "Rscript", "-e", code],
            capture_output=True, text=True, timeout=timeout,
        )

    # -- ref parsing -----------------------------------------------------------

    @staticmethod
    def _parse(dep: str) -> dict:
        dep = dep.strip()
        if "/" in dep:
            repo, _, ref = dep.partition("@")
            return {"kind": "github", "repo": repo, "ref": ref or "HEAD"}
        parts = dep.split()
        if len(parts) == 1:
            return {"kind": "cran", "name": parts[0], "version": None}
        name, constraint = parts[0], " ".join(parts[1:])
        if constraint.startswith("=="):
            return {"kind": "cran", "name": name,
                    "version": constraint[2:].strip()}
        raise WeftError(
            "task.invalid", f"cran constraint {dep!r} not supported",
            stage="solve",
            hints={"supported": ["name", "name ==X.Y.Z", "owner/repo@ref"],
                   "suggestion": "the snapshot date pins everything else; "
                                 "use ==X.Y.Z only to assert an expectation"},
        )

    @staticmethod
    def _github_resolve(repo: str, ref: str) -> dict:
        import json as _json
        import urllib.request

        def get(url):
            req = urllib.request.Request(url, headers={"User-Agent": "weft"})
            return urllib.request.urlopen(req, timeout=30).read()

        try:
            sha = _json.loads(
                get(f"https://api.github.com/repos/{repo}/commits/{ref}")
            )["sha"]
            desc = get(
                f"https://raw.githubusercontent.com/{repo}/{sha}/DESCRIPTION"
            ).decode()
        except Exception as e:
            raise WeftError(
                "env.solve_conflict",
                f"cannot resolve github ref {repo}@{ref}",
                stage="solve",
                hints={"ecosystem": "cran", "user_pins": [f"{repo}@{ref}"],
                       "solver_message": str(e)[-300:],
                       "suggestion": "check repo/ref exist and are public; "
                                     "an R package needs DESCRIPTION at root"},
            ) from e
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
                "repo": repo, "ref": ref, "deps": deps}

    # -- Solver interface --------------------------------------------------------

    def solve(self, deps: list[str], spec, workdir: Path) -> dict:
        import datetime
        import json as _json
        workdir.mkdir(parents=True, exist_ok=True)
        parsed = [self._parse(d) for d in deps]
        gh = [self._github_resolve(p["repo"], p["ref"])
              for p in parsed if p["kind"] == "github"]
        cran_direct = [p for p in parsed if p["kind"] == "cran"]
        want = [p["name"] for p in cran_direct] + \
               [d for g in gh for d in g["deps"]]
        # a dated snapshot is the reproducibility anchor: same date, same
        # answers, forever. Pin via system_requirements.cran_snapshot;
        # otherwise today's date is captured at first solve.
        date = (getattr(spec, "system_requirements", {}) or {}).get(
            "cran_snapshot") or datetime.date.today().isoformat()
        snapshot = self.PPM.format(date=date)
        out = workdir / "closure.tsv"
        code = (
            'options(repos=c(CRAN={snap}));'
            'ap <- available.packages();'
            'base <- rownames(installed.packages(priority=c("base","recommended")));'
            'want <- setdiff(c({want}), c(base, ""));'
            'miss <- setdiff(want, rownames(ap));'
            'if (length(miss)) {{ write(paste("MISSING:", paste(miss, collapse=",")), stderr()); quit(status=3) }};'
            'cl <- unique(c(want, unlist(tools::package_dependencies(want, db=ap, recursive=TRUE))));'
            'cl <- setdiff(cl, base); cl <- intersect(cl, rownames(ap));'
            'write.table(ap[cl, c("Package","Version"), drop=FALSE], {out}, sep="\\t", '
            'row.names=FALSE, col.names=FALSE, quote=FALSE)'
        ).format(snap=_json.dumps(snapshot),
                 want=", ".join(_json.dumps(x) for x in want) or '""',
                 out=_json.dumps(str(out)))
        if want:
            r = self._rscript(code)
            if r.returncode != 0:
                msg = (r.stderr or r.stdout)[-1200:]
                raise WeftError(
                    "env.solve_conflict",
                    "cran layer is unsatisfiable against the snapshot",
                    stage="solve",
                    hints={"ecosystem": "cran", "user_pins": deps,
                           "snapshot": snapshot, "solver_message": msg,
                           "suggestion": "package name typo, or not on CRAN "
                                         "— use owner/repo@ref for github"},
                )
            rows = [l.split("\t") for l in out.read_text().splitlines() if l]
        else:
            rows = []
        records = [{"name": n, "version": v, "source": snapshot, "sha256": ""}
                   for n, v in rows]
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
                "source": f'github:{g["repo"]}@{g["ref"]}',
                "remote_sha": g["sha"], "sha256": "",
                "tarball": f'https://codeload.github.com/{g["repo"]}'
                           f'/tar.gz/{g["sha"]}',
            })
        records.sort(key=lambda x: (x["name"], x["version"]))
        gh_names = {g["name"] for g in gh}
        return {"records": records, "snapshot": snapshot,
                "native": _json.dumps({"snapshot": snapshot,
                                       "records": records}, indent=1),
                "from_source": sorted(gh_names),
                "top_level": sorted({p["name"] for p in cran_direct}
                                    | gh_names)}

    def realize_layer(self, layer: dict, adapter, env_rel: str) -> str:
        import json as _json
        import shlex
        env_dir = adapter.path(env_rel)
        rlib = f"{env_dir}/rlib"
        cran_names = [r["name"] for r in layer["records"]
                      if not r.get("remote_sha")]
        tarballs = [r["tarball"] for r in layer["records"]
                    if r.get("remote_sha")]
        top = layer.get("top_level", [])
        rcode = (
            'options(repos=c(CRAN={snap}), HTTPUserAgent=sprintf('
            '"R/%s R (%s)", getRversion(), paste(getRversion(), '
            'R.version$platform, R.version$arch, R.version$os)));'
            'lib <- {lib}; dir.create(lib, showWarnings=FALSE, recursive=TRUE);'
            'p <- c({pkgs}); p <- setdiff(p, rownames(installed.packages(lib.loc=lib)));'
            'if (length(p)) install.packages(p, lib=lib);'
            # PPM linux binaries assume focal-era glibc; on older hosts they
            # install but fail to *load* — detect and rebuild those from source
            'chk <- intersect(c({pkgs}), rownames(installed.packages(lib.loc=lib)));'
            'bad <- Filter(function(x) inherits(tryCatch('
            'loadNamespace(x, lib.loc=lib), error=function(e) e), "error"), chk);'
            'if (length(bad)) {{'
            ' write(paste("binary load failed, rebuilding from source:",'
            ' paste(bad, collapse=",")), stderr());'
            ' srcrepo <- sub("__linux__/[^/]+/", "", {snap});'
            ' remove.packages(bad, lib=lib);'
            ' install.packages(bad, lib=lib, repos=srcrepo, type="source") }};'
            't <- c({tarballs});'
            'if (length(t)) install.packages(t, lib=lib, repos=NULL, type="source");'
            'need <- c({top});'
            'ok <- need %in% rownames(installed.packages(lib.loc=lib));'
            'if (!all(ok)) {{ write(paste("FAILED:", paste(need[!ok], collapse=",")), stderr()); quit(status=4) }}'
        ).format(snap=_json.dumps(layer["snapshot"]),
                 lib=_json.dumps(rlib),
                 pkgs=", ".join(_json.dumps(x) for x in cran_names) or '""',
                 tarballs=", ".join(_json.dumps(x) for x in tarballs) or 'character(0)',
                 top=", ".join(_json.dumps(x) for x in top) or 'character(0)')
        r = adapter.run_cmd(
            f". {shlex.quote(env_dir)}/activate.sh && "
            f"Rscript -e {shlex.quote(rcode)}",
            timeout=3600,
        )
        if r.rc != 0:
            raise WeftError(
                "env.realize_failed",
                "cran layer install failed on site",
                stage="realize",
                hints={"ecosystem": "cran",
                       "log_tail": (r.err or r.out)[-1500:],
                       "note": "cran realization needs network from the "
                               "install point in v1; on air-gapped sites "
                               "prefer conda-forge r-<name> packages or "
                               "build R packages as tasks"},
            )
        return f'export R_LIBS="{rlib}' + '${R_LIBS:+:$R_LIBS}"'

    def why(self, env_row: dict, package: str, workdir: Path) -> str:
        return f"{package}: see the cran layer record (env_why returns it)"


def default_solvers(pixi_bin: str) -> dict[str, object]:
    """The registry. Adding an ecosystem = one class + one entry here
    (or inject externally via Weft(solvers={...}))."""
    return {
        "conda": PixiSolver(pixi_bin),
        "cran": CranSolver(pixi_bin),
    }


def check_layer_requirements(spec, layers_present: dict[str, list[str]],
                             solvers: dict[str, object]) -> None:
    """Generic cross-layer contract: each solver declares what it needs
    from the conda layer; checked before any solving is paid for."""
    conda_names = {d.split()[0].lower() for d in spec.conda}
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
