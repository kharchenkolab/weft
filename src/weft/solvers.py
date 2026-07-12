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
            # per-package direct deps: the graph offline installs need for
            # topological ordering (packed layers, design-next B2)
            'dg <- tools::package_dependencies(cl, db=ap, recursive=FALSE);'
            'dgs <- vapply(cl, function(p) paste(setdiff(intersect(dg[[p]], cl), base), collapse=","), "");'
            'write.table(data.frame(ap[cl, "Package"], ap[cl, "Version"], dgs), {out}, sep="\\t", '
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
        records = [{"name": r[0], "version": r[1], "source": snapshot,
                    "sha256": "",
                    "deps": [d for d in (r[2] if len(r) > 2 else ""
                                         ).split(",") if d]}
                   for r in rows]
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
        r = adapter.run_activated(
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
        rlib = f"{env_dir}/rlib"
        parent_rlib = f"{parent_dir}/rlib"

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
            parent_env_id, "linux-64")
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

        cran_names = [r["name"] for r in recs if not r.get("remote_sha")]
        tarballs = [r["tarball"] for r in recs if r.get("remote_sha")]
        rcode = (
            'options(repos=c(CRAN={snap}), HTTPUserAgent=sprintf('
            '"R/%s R (%s)", getRversion(), paste(getRversion(), '
            'R.version$platform, R.version$arch, R.version$os)));'
            'lib <- {lib}; dir.create(lib, showWarnings=FALSE, recursive=TRUE);'
            '.libPaths(c(lib, {plib}, .libPaths()));'
            'p <- c({pkgs});'
            'if (length(p)) install.packages(p, lib=lib);'
            't <- c({tarballs});'
            'if (length(t)) install.packages(t, lib=lib, repos=NULL, type="source");'
            'need <- c({need});'
            'ok <- need %in% rownames(installed.packages(lib.loc=lib));'
            'if (!all(ok)) {{ write(paste("FAILED:", paste(need[!ok], collapse=",")), stderr()); quit(status=4) }}'
        ).format(snap=_json.dumps(layer["snapshot"]),
                 lib=_json.dumps(rlib), plib=_json.dumps(parent_rlib),
                 pkgs=", ".join(_json.dumps(x) for x in cran_names) or 'character(0)',
                 tarballs=", ".join(_json.dumps(x) for x in tarballs) or 'character(0)',
                 need=", ".join(_json.dumps(x) for x in added) or 'character(0)')
        r = adapter.run_activated(
            prelude +
            f". {shlex.quote(parent_dir)}/activate.sh && "
            f"Rscript -e {shlex.quote(rcode)} 2>&1", timeout=3600)
        if r.rc != 0:
            raise WeftError(
                "env.realize_failed",
                "cran overlay layer install failed",
                stage="realize",
                hints={"ecosystem": "cran",
                       "log_tail": (r.err or r.out)[-1500:],
                       "note": "if the package needs a native library the "
                               "parent lacks, it cannot be layered: add that "
                               "conda package to the parent env"})
        # populate the compile cache for everyone who comes next
        if store is not None and cas is not None:
            import tempfile
            from pathlib import Path as _P
            tar_rel = f"{env_rel}/rlib-cache.tar"
            adapter.run_cmd(
                f"tar -cf {shlex.quote(adapter.path(tar_rel))} "
                f"-C {shlex.quote(rlib)} .", timeout=600)
            try:
                data = adapter.read_file(tar_rel)
                with tempfile.TemporaryDirectory() as td:
                    p = _P(td) / "rlib.tar"
                    p.write_bytes(data)
                    ref = put_cached_build(store, cas, key, p)
                store.emit("overlay.compile_cached", key=key, ref=ref,
                           packages=added)
            except WeftError:
                pass          # caching is an optimization, never a failure
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
                else:                            # cran: source tarball
                    base = layer["snapshot"].replace("__linux__/focal/", "")
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
                       "note": "packages build from source on the site; the "
                               "conda layer must provide the toolchain "
                               "(c-compiler, fortran-compiler, make)"})
        return f'export R_LIBS="{rlib}' + '${R_LIBS:+:$R_LIBS}"'

    def why(self, env_row: dict, package: str, workdir: Path) -> str:
        return f"{package}: see the cran layer record (env_why returns it)"


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
        manifest.write_text(
            '[workspace]\nname = "weft-julia-solver"\n'
            'channels = ["conda-forge"]\nplatforms = ["linux-64"]\n\n'
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

    def solve(self, deps: list[str], spec, workdir: Path) -> dict:
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
            raise WeftError(
                "env.solve_conflict",
                "julia layer is unsatisfiable as pinned",
                stage="solve",
                hints={"ecosystem": "julia", "user_pins": deps,
                       "solver_message": (r.stderr or r.stdout)[-1200:]})
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
            name = dep.split()[0].split("=")[0].strip()
            rec = by_name.get(name)
            if rec and rec.get("version"):
                pins.append(f'{name} =={rec["version"]}')
            else:
                pins.append(dep)
        return pins, {}

    def realize_layer(self, layer: dict, adapter, env_rel: str) -> str:
        import shlex
        env_dir = adapter.path(env_rel)
        proj, _, man = layer["native"].partition("\n###WEFT-MANIFEST###\n")
        adapter.write_file(f"{env_rel}/julia/Project.toml", proj.encode())
        adapter.write_file(f"{env_rel}/julia/Manifest.toml", man.encode())
        depot = adapter.path("cache/julia-depot")
        r = adapter.run_activated(
            f". {shlex.quote(env_dir)}/activate.sh && "
            f"JULIA_DEPOT_PATH={shlex.quote(depot)} "
            f"julia --project={shlex.quote(env_dir + '/julia')} "
            f"-e 'using Pkg; Pkg.instantiate()'",
            timeout=3600)
        if r.rc != 0:
            raise WeftError(
                "env.realize_failed", "julia layer instantiate failed on site",
                stage="realize",
                hints={"ecosystem": "julia",
                       "log_tail": (r.err or r.out)[-1500:],
                       "note": "julia realization needs network from the "
                               "install point in v1"})
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
        depot = adapter.path("cache/julia-depot")
        r = adapter.run_activated(
            f". {shlex.quote(parent_dir)}/activate.sh && "
            f"JULIA_DEPOT_PATH={shlex.quote(depot)} "
            f"julia --project={shlex.quote(env_dir + '/julia')} "
            f"-e 'using Pkg; Pkg.instantiate()'",
            timeout=3600)
        if r.rc != 0:
            raise WeftError(
                "env.realize_failed",
                "julia overlay instantiate failed on site", stage="realize",
                hints={"ecosystem": "julia",
                       "log_tail": (r.err or r.out)[-1500:]})
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
