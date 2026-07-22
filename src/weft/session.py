"""Session environments: mutable, unhashed, single-site scratch (doc 03 §7).

The interactive loop — "try importing X… now add Y" — gets a scratch clone
of a realized environment that the agent may mutate incrementally via
`pixi add` (seconds against a warm package cache). The rules that protect
the identity model:

  * session envs are unhashed and single-site; nothing that runs in one can
    enter the project record (session_exec returns output, never manifests)
  * `snapshot` synthesizes the minimal spec delta over the base, re-solves
    properly, and returns a real EnvID — the citable re-run is then cheap
    because every package is already in the site cache.

Sessions need index access from the site; on air-gapped sites the error
says so instead of half-working.
"""

from __future__ import annotations

import json as _json
import shlex
import uuid

from .adapters.base import SiteAdapter
from .envman import EnvManager
from .errors import WeftError
from .realize import env_dir_rel
from .store import Store


def _r_install_failure(text: str) -> tuple[str, bool, str]:
    """Map an R install failure to WHICH of three different problems it
    was — they pull three different agent levers (2026-07 field note).
    The R invocations run under LC_ALL=C so these markers are stable."""
    if "unable to access index for repository" in text:
        return ("env.solve_failed", True,
                "an R repository index is unreachable from this node — "
                "network/proxy trouble, not a package problem")
    if "is not available" in text:   # "package 'X' is not available ..."
        return ("env.solve_conflict", False,
                "requested package(s) not present in the configured "
                "repositories/snapshot")
    if "cannot open URL" in text and "api.github.com" in text:
        # standalone remotes prints BYTE-IDENTICAL text for a missing
        # repo and a dead network (probed 2026-07-22) — the caller
        # disambiguates with a controller-side github resolve
        return ("env.solve_failed", True,
                "a github fetch failed on the site — missing repo and "
                "unreachable github look identical from remotes")
    return ("env.realize_failed", False,
            "installing the R delta into the session layer failed")


def _pixi_add_failure(text: str) -> tuple[str, bool, str, str]:
    """(code, retryable, why, stage) for a failed session `pixi add`,
    keyed on lock.py's field-verified pixi markers. This was the last
    undiscriminated catch-all wearing env.solve_conflict (2026-07 sweep
    A2) — a manifest parse failure, an index outage, and a pip build
    failure all landed there wearing 'your packages conflict'."""
    from .lock import (_CACHE_MARKERS, _CONFLICT_MARKERS, _NETWORK_MARKERS,
                       _PARSE_RE)
    low = text.lower()
    if "duplicate key" in low or _PARSE_RE.search(text):
        return ("internal.error", False,
                "the session manifest no longer parses — not a package "
                "problem", "realize")
    if any(m.lower() in low for m in _CONFLICT_MARKERS):
        return ("env.solve_conflict", False,
                "the requested packages cannot be added to the session "
                "as pinned", "solve")
    if any(m in low for m in _NETWORK_MARKERS + _CACHE_MARKERS):
        return ("env.solve_failed", True,
                "pixi could not reach or read its indexes from this "
                "site", "solve")
    return ("env.realize_failed", False,
            "incremental install failed in session", "realize")


def _pip_failure(text: str, default: str = "env.realize_failed",
                 default_retryable: bool = False) -> tuple[str, bool]:
    """Same discrimination for pip: a spec conflict, a dead index, and a
    broken install are different failures with different levers — none
    of them is 'unsatisfiable spec' by default."""
    if any(m in text for m in ("ResolutionImpossible",
                               "No matching distribution",
                               "Could not find a version that satisfies")):
        return "env.solve_conflict", False
    if any(m in text for m in ("Could not fetch URL", "NewConnectionError",
                               "ReadTimeoutError", "ProxyError",
                               "Temporary failure in name resolution",
                               "Connection broken", "timed out")):
        return "env.solve_failed", True
    return default, default_retryable


class SessionManager:
    def __init__(self, store: Store, envman: EnvManager, runner=None,
                 dataman=None, adapters=None):
        self.store = store
        self.envman = envman
        self.runner = runner   # for auto-realizing a base env (ergonomics)
        self.dataman = dataman   # for content-addressing installer sources
        self._adapters = adapters

    def start(self, base: str | dict, adapter: SiteAdapter) -> dict:
        """Accepts an EnvID *or a spec* — exploration should not cost three
        round trips (ensure → throwaway task to realize → start).

        LAZY CLONE (parallel-FS round): a session buys mutability, and
        the writable clone is its price — so the price is paid at the
        first MUTATION (session_install / run_installer), not at start.
        Until then, execution attaches to the base realization in place —
        including an adopted read-only squashfs pack at its recorded
        location — so a no-additions session lays down no per-session
        prefix (a ~10^5-file hardlink forest that costs minutes on
        BeeGFS/Lustre and defeats the mount it shadows)."""
        if isinstance(base, dict):
            base_env_id = self.envman.ensure(base)["env_id"]
        else:
            base_env_id = base
        env_row = self.store.get_env(base_env_id)
        if not env_row:
            raise WeftError("task.invalid", f"unknown EnvID: {base_env_id}", stage="solve")
        site_row = self.store.get_site(adapter.name)
        caps = (site_row or {}).get("capabilities") or {}
        from .capability import compute_view
        # index access is a fact to SURFACE at start and a requirement to
        # ENFORCE at the first install — a no-additions session on an
        # air-gapped site is perfectly serviceable
        no_index = not compute_view(caps).get("internet", False)
        # the RECORDED location: an adopted read-only realization lives
        # outside the writable root (env_dir_rel is only the default)
        real = self.store.get_realization(base_env_id, adapter.name)
        base_loc = (real or {}).get("location") or env_dir_rel(base_env_id)
        if not adapter.file_exists(f"{base_loc}/.weft-ready"):
            # realizing the base is weft's errand, not the agent's
            if self.runner is None:
                raise WeftError(
                    "env.not_realized",
                    f"env {base_env_id} is not realized on {adapter.name}",
                    stage="realize",
                    hints={"suggestion": "run any task with it there first"})
            from .realize import ensure_realization
            ensure_realization(
                base_env_id, env_row, adapter, self.store,
                caps=(site_row or {}).get("capabilities"),
                site_config=(site_row or {}).get("config"),
                pack_tools={"pixi_pack": self.runner.pixi_pack,
                            "cas": self.runner.cas,
                            "transfers": self.runner.transfers,
                            "solvers": self.envman.solvers,
                            "store": self.store,
                            "dataman": self.runner.dataman})
        session_id = "ses_" + uuid.uuid4().hex[:10]
        rel = f"sessions/{session_id}"
        # a home for kernels/sidecars now, the prefix later (on demand)
        adapter.run_cmd(f"mkdir -p {shlex.quote(adapter.path(rel))}",
                        timeout=60)
        self.store.put_session(session_id, base_env_id, adapter.name, rel,
                               materialized=False)
        self.store.emit("session.started", session=session_id,
                        base=base_env_id, site=adapter.name, lazy=True)
        out = {
            "session_id": session_id, "site": adapter.name,
            "base_env_id": base_env_id,
            "materialized": False,
            "runtime": self.runtime(self.store.get_session(session_id),
                                    adapter),
            "note": "running from the base realization; a writable "
                    "prefix is cloned at the first session_install",
            "warning": "unhashed scratch environment — snapshot before "
                       "recording any result",
        }
        if no_index:
            out["warning"] += ("; no package-index access from "
                               f"{adapter.name} — installs will refuse")
        return out

    def _materialize(self, s: dict, adapter: SiteAdapter) -> bool:
        """Clone the writable per-session prefix (manifest + lock from
        the STORE — an overlay realization has no pixi files of its own;
        the install is a hardlink forest from the shared package cache).
        Deferred to the first mutation. Returns True iff this call did
        the clone."""
        if s.get("materialized", True):
            return False
        site_row = self.store.get_site(adapter.name)
        from .capability import compute_view
        if not compute_view((site_row or {}).get("capabilities")
                            or {}).get("internet", False):
            raise WeftError(
                "env.unsatisfiable_on_site",
                f"session installs need package-index access from "
                f"{adapter.name}",
                stage="realize",
                hints={"suggestion": "extend the spec instead and let weft "
                                     "deliver a packed realization"})
        env_row = self.store.get_env(s["base_env_id"])
        rel = s["location"]
        adapter.write_file(f"{rel}/pixi.toml", env_row["manifest"].encode())
        adapter.write_file(f"{rel}/pixi.lock", env_row["native_lock"].encode())
        from .realize import _virtual_pkg_overrides
        r = adapter.run_cmd(
            _virtual_pkg_overrides(env_row) +
            f"{shlex.quote(adapter.pixi_bin)} install "
            f"--manifest-path {shlex.quote(adapter.path(rel))}/pixi.toml",
            timeout=900,
        )
        if r.rc != 0:
            raise WeftError(
                "env.realize_failed", "session clone failed", stage="realize",
                hints={"log_tail": (r.err or r.out)[-1000:]},
            )
        self.store.set_session_materialized(s["session_id"])
        s["materialized"] = True
        self.store.emit("session.materialized", session=s["session_id"],
                        base=s["base_env_id"])
        return True

    @staticmethod
    def _exec_template(activation: str, ns_wrap: bool) -> str:
        """A ready-to-exec prefix: `shlex.split(template) + argv` runs
        argv INSIDE the session's activated env, on the session's SITE.
        Closes the trap a consumer hit: prefix paths on mount-adopted
        bases exist only inside the activation's namespace, so a bare
        exec dies illegibly — this string is the thing you CAN exec.
        bash is preferred for the activation (conda activate.d carries
        bashisms el7's sh refuses); the unshare layer rides along when
        the mount lives in a namespace."""
        inner = f"{activation} && exec \"$@\""
        dispatch = ('if command -v bash >/dev/null 2>&1; '
                    'then exec bash -c "$0" bash "$@"; '
                    'else exec sh -c "$0" sh "$@"; fi')
        t = f"sh -c {shlex.quote(dispatch)} {shlex.quote(inner)}"
        return f"unshare -rm {t}" if ns_wrap else t

    def runtime(self, s: dict | str, adapter: SiteAdapter) -> dict:
        """What does this session RUN FROM right now — the authoritative
        contract for callers that exec interpreters themselves (aba's
        launcher lanes), so nobody rederives substrate internals.

        `activation` is ALWAYS correct: source it, then exec (inside
        `unshare -rm` when ns_wrap — squashfs mounts live only in the
        activation's namespace). `prefix` is informational; for squashfs
        bases it is MOUNT-SCOPED and does not exist outside activation.
        `direct_exec` says when prefix/bin/* may be exec'd without
        activation (plain on-disk prefixes only). `env_id` is the
        identity of what's active: the base env pre-clone, and NULL once
        mutated — unhashed scratch has no identity until snapshot."""
        if isinstance(s, str):
            # observation, NOT activity: bypass _get's touch_session —
            # a monitoring loop polling runtime must not keep an idle
            # session looking active to session_idle_days
            row = self.store.get_session(s)
            if not row or row["state"] != "active":
                raise WeftError("task.invalid", f"no active session {s}",
                                stage="infra")
            s = row
        mode = s.get("materialize_mode",
                     "clone" if s.get("materialized", True) else "none")
        if mode == "clone":
            manifest = adapter.path(f"{s['location']}/pixi.toml")
            out = {
                "source": "session",
                "env_id": None,
                "prefix": adapter.path(
                    f"{s['location']}/.pixi/envs/default"),
                "activation": (
                    f"eval \"$({shlex.quote(adapter.pixi_bin)} shell-hook "
                    f"--manifest-path {shlex.quote(manifest)})\""),
                "ns_wrap": False,
                "direct_exec": True,
            }
            if s.get("added_cran"):
                overlay = adapter.path(f"{s['location']}/overlay.sh")
                out.update(
                    activation=out["activation"]
                    + f" && . {shlex.quote(overlay)}",
                    direct_exec=False,
                    rlib=adapter.path(f"{s['location']}/rlib"))
            out["exec_template"] = self._exec_template(
                out["activation"], False)
            return out
        act, ns = self._base_activation(s, adapter)
        real = self.store.get_realization(s["base_env_id"],
                                          adapter.name) or {}
        loc = real.get("location") or env_dir_rel(s["base_env_id"])
        strategy = real.get("strategy") or "prefix"
        if "squashfs" in strategy:
            prefix = adapter.path(f"{loc}/mnt/.pixi/envs/default")
        elif strategy == "prefix":
            prefix = adapter.path(f"{loc}/.pixi/envs/default")
        else:
            # packed/overlay/modules layouts vary — activation is the
            # contract; never hand out a path we cannot vouch for
            prefix = None
        out = {
            "source": "base",
            "env_id": s["base_env_id"],
            "prefix": prefix,
            "activation": act,
            "ns_wrap": ns,
            "direct_exec": strategy == "prefix" and not ns,
        }
        has_cran = bool(s.get("added_cran"))
        if mode == "pylib" or has_cran:
            # the session's own layer(s) ride the base: activation
            # composes them, the content is mutated (no identity), and
            # direct exec cannot see env-var composition
            overlay = adapter.path(f"{s['location']}/overlay.sh")
            out.update(
                env_id=None,
                activation=f"{act} && . {shlex.quote(overlay)}",
                direct_exec=False)
            if mode == "pylib":
                out["pylib"] = adapter.path(f"{s['location']}/pylib")
            if has_cran:
                out["rlib"] = adapter.path(f"{s['location']}/rlib")
        out["exec_template"] = self._exec_template(out["activation"], ns)
        return out

    def _base_activation(self, s: dict, adapter: SiteAdapter) -> tuple[str, bool]:
        """Activation line for the base realization (pre-clone lane) and
        whether the script must run inside a mount namespace (squashfs
        bases mount lazily; the mount must die with the command)."""
        real = self.store.get_realization(s["base_env_id"], adapter.name)
        loc = (real or {}).get("location") or env_dir_rel(s["base_env_id"])
        act = f". {shlex.quote(adapter.path(loc))}/activate.sh"
        ns = bool(self.runner is not None
                  and self.runner.ns_wrap_needed(s["base_env_id"],
                                                 adapter.name))
        return act, ns

    def _stack_activation(self, s: dict,
                          adapter: SiteAdapter) -> tuple[str, bool]:
        """Activation of the session's interpreter stack per mode —
        clone: the live prefix; none/pylib: the base realization."""
        mode = s.get("materialize_mode",
                     "clone" if s.get("materialized", True) else "none")
        if mode == "clone":
            manifest = adapter.path(f"{s['location']}/pixi.toml")
            return (f"eval \"$({shlex.quote(adapter.pixi_bin)} shell-hook "
                    f"--manifest-path {shlex.quote(manifest)})\""), False
        return self._base_activation(s, adapter)

    def _ensure_overlay_line(self, s: dict, adapter: SiteAdapter,
                             line: str) -> None:
        """overlay.sh is the ONE composition artifact (PYTHONPATH for
        pylib, R_LIBS for rlib — layers coexist): append the line if it
        is not already there."""
        rel = f"{s['location']}/overlay.sh"
        try:
            current = adapter.read_file(rel).decode()
        except WeftError:
            current = ""
        if line not in current:
            adapter.write_file(rel, (current + line + "\n").encode())

    def _materialize_rlib(self, s: dict, adapter: SiteAdapter,
                          cran: list[str],
                          extra_repos: list[str] | None = None) -> dict:
        """The R layer: install into a session-owned rlib composed via
        R_LIBS — R's installer checks every .libPaths() entry and skips
        base-satisfied dependencies natively, so this is delta-only on
        ANY base (frozen or built-here) with no clone and no two-phase
        dance. The same mechanism the citable overlay path uses
        (solvers.CranSolver.realize_overlay); the snapshot's solve pins
        versions — session installs are unpinned scratch by doctrine."""
        act, ns = self._stack_activation(s, adapter)
        sdir = adapter.path(s["location"])
        rlib = f"{sdir}/rlib"
        from .realize import _ns_wrap_cmd
        wrap = _ns_wrap_cmd if ns else (lambda x: x)
        site_row = self.store.get_site(adapter.name) or {}
        default_repo = (site_row.get("config") or {}).get(
            "cran_repos", "https://cloud.r-project.org")
        repo_urls = list(dict.fromkeys(
            list(extra_repos or []) + [default_repo]))
        # every caller string entering R code goes through json.dumps —
        # the same parity solvers.py keeps; a bare quote in a name/url/
        # ref would otherwise break OUT of the R string (injection sweep)
        repos_vec = ", ".join(_json.dumps(u) for u in repo_urls)
        # ONE vocabulary — the same strings deps.cran takes: plain names,
        # "name ==X.Y.Z" (pin asserted at the snapshot's solve), and
        # "owner/repo@ref" github sources (the solver SHA-pins those)
        # ONE grammar (spec.parse_cran_dep) — this lane used to accept
        # any operator by silently reducing to the bare name, and the
        # snapshot then re-emitted a string the solve lane REFUSES: a
        # working session minted an unsnapshottable state (2026-07
        # vocabulary sweep #2). Unsupported shapes now refuse here, at
        # install time, with the solve lane's exact contract.
        from .spec import parse_cran_dep
        plain, refs, pin_notes = [], [], []
        for c in cran:
            p = parse_cran_dep(c)
            if p["kind"] == "github":
                refs.append(c)
                continue
            plain.append(p["name"])
            if p["version"]:
                pin_notes.append(
                    f'{p["name"]}: sessions install the repository '
                    f'latest (unpinned scratch by doctrine); the '
                    f'recorded =={p["version"]} is asserted at '
                    f'session_snapshot against the dated snapshot')
        vec = ", ".join(_json.dumps(n) for n in plain)
        # CRAN routinely compiles: bring weft's toolchain like the
        # overlay build does (best-effort — pure-R needs none)
        prelude = ""
        try:
            from .toolchain import build_env_prelude, ensure_toolchain
            tc = ensure_toolchain(adapter, adapter.pixi_bin)
            if tc:
                prelude = build_env_prelude(adapter, tc, sdir)
        except WeftError:
            pass
        parts = [f"repos <- c({repos_vec})"]
        if plain:
            parts.append(
                f"install.packages(c({vec}), lib=\"{rlib}\", repos=repos)")
        if refs:
            # remotes bootstraps itself into the session layer once;
            # install_github RAISES on failure (unlike install.packages,
            # which only warns), so refs get honest rc propagation free
            parts.append(
                'if (!requireNamespace("remotes", quietly=TRUE)) '
                f'install.packages("remotes", lib="{rlib}", repos=repos)')
            for ref in refs:
                # capture the RETURNED package name (repo tail is not the
                # package name; subdir specs even less so) — and because
                # install_github does NOT raise for compile-stage R CMD
                # INSTALL failures (field report: returns 0, package
                # removed), the marker is the positive confirmation the
                # verification below keys on
                parts.append(
                    f'.nm <- remotes::install_github({_json.dumps(ref)}, '
                    f'lib={_json.dumps(rlib)}, '
                    f'upgrade="never", repos=repos); '
                    'cat("\nWEFT-INSTALLED ", '
                    'paste(.nm, collapse=" "), "\n", sep="")')
        rcmd = "; ".join(parts)
        script = (prelude + act + " && "
                  f"mkdir -p {shlex.quote(rlib)} && "
                  f"export R_LIBS={shlex.quote(rlib)}\"${{R_LIBS:+:$R_LIBS}}\" && "
                  # standalone: remotes uses ONLY base R — no callr/
                  # processx helper stack, which a lean base env lacks
                  # (cbe field find: install_github died wanting callr)
                  "export R_REMOTES_STANDALONE=true && "
                  # C locale: failure classification keys on R's message
                  # text ("unable to access index...") — a translated
                  # message would dodge the classifier
                  "export LC_ALL=C LANGUAGE=C && "
                  f"Rscript -e {shlex.quote(rcmd)} 2>&1")
        try:
            r = adapter.run_activated(wrap(script), timeout=3600)
        except WeftError as e:
            raise WeftError(
                "env.realize_failed",
                "fetching/compiling the R delta stalled or timed out",
                stage="realize", retryable=True,
                hints={"requested": cran, "detail": e.detail}) from e
        # install.packages WARNS but exits 0 on failure — verify by
        # presence, or a broken add would report success (R's trap)
        ref_installed: list[str] = []
        if refs and r.rc == 0:
            marker_lines = [ln for ln in r.out.splitlines()
                            if ln.startswith("WEFT-INSTALLED ")]
            for ln in marker_lines:
                ref_installed += ln.split()[1:]
            if len(marker_lines) < len(refs):
                # returned 0 without confirming every ref: the silent
                # compile-failure shape — fail LOUDLY with the build
                # tail. This is a BUILD failure after successful
                # resolution, not an unsatisfiable spec.
                raise WeftError(
                    "env.realize_failed",
                    "a github R install returned without raising and "
                    "without confirming the package — compile-stage "
                    "failures do not raise; treating as failed",
                    stage="realize",
                    hints={"requested": cran, "confirmed": ref_installed,
                           "out_tail": r.out[-1500:],
                           "err_tail": r.err[-800:]})
        verify_names = plain + ref_installed
        v_rc, v_out = 0, ""
        if verify_names and r.rc == 0:
            vvec = ", ".join(_json.dumps(n) for n in verify_names)
            vcmd = (f'missing <- setdiff(c({vvec}), '
                    f'basename(list.dirs("{rlib}", recursive=FALSE))); '
                    f'if (length(missing)) {{ '
                    f'cat("MISSING:", missing, "\\n"); quit(status=1) }}')
            v = adapter.run_activated(wrap(
                f"{act} && Rscript -e {shlex.quote(vcmd)}"), timeout=120)
            v_rc, v_out = v.rc, v.out
        if r.rc != 0 or v_rc != 0:
            # WHICH failure decides the code (dead index / not-in-repo /
            # broken build are different levers), and every rc says
            # WHOSE rc it is — a bare {"rc": 0} in a raised failure sent
            # the field agent hunting a phantom (2026-07 note, #1/#2)
            code, retryable, why = _r_install_failure(
                r.out + r.err + v_out)
            missing_line = next((ln for ln in v_out.splitlines()
                                 if ln.startswith("MISSING:")), "")
            hints = {"requested": cran, "install_rc": r.rc,
                     "verify_rc": v_rc,
                     "out_tail": r.out[-1200:], "err_tail": r.err[-800:],
                     "script_tail": rcmd[-400:]}
            if missing_line:
                hints["missing"] = missing_line
            if (code == "env.solve_failed" and refs
                    and "api.github.com" in r.out + r.err):
                # the ambiguous github-fetch shape: resolve each ref from
                # the CONTROLLER, which does see HTTP statuses — a
                # missing repo becomes a spec verdict, a live repo proves
                # the failure was the site's egress
                from .solvers import CranSolver
                hints["checked_from_controller"] = True
                for ref in refs:
                    p = CranSolver._parse(ref)
                    try:
                        CranSolver._github_resolve(p["repo"], p["ref"],
                                                   p.get("subdir"))
                    except WeftError as ge:
                        if ge.code == "env.solve_conflict":
                            code, retryable = "env.solve_conflict", False
                            why = (f"github ref {ref!r} does not resolve "
                                   "(verified from the controller) — the "
                                   "fetch failure was the missing "
                                   "repo/ref, not the network")
                            hints["bad_ref"] = ref
                        else:
                            why += ("; the controller cannot reach "
                                    "github either")
                        break
                else:
                    why += ("; every ref resolves from the controller — "
                            "the SITE's github egress failed")
            raise WeftError(code, why, stage="realize",
                            retryable=retryable, hints=hints)
        self._ensure_overlay_line(
            s, adapter,
            f'export R_LIBS="{rlib}' + '${R_LIBS:+:$R_LIBS}"')
        installed = plain + refs
        self.store.emit("session.installed", session=s["session_id"],
                        cran=installed, mode="rlib")
        out = {"rlib": rlib, "installed": installed}
        if ref_installed:
            out["resolved"] = ref_installed   # DESCRIPTION package names
        if pin_notes:
            out["pin_notes"] = pin_notes      # honest: pins are recorded,
        return out                            # not enforced, until snapshot

    def _postconditions(self, session_id: str, adapter: SiteAdapter,
                        out: dict, eff: dict, conda: list[str],
                        pypi: list[str], cran: list[str]) -> dict:
        """Run the postcondition in the composed runtime and GATE the
        records: entries whose verification did not PASS are retracted
        from the session's recorded deps (the disk residue stays —
        idempotent scratch; the snapshot must only carry what is true).
        A crash between record and retraction leaves an unverified
        record for one call — the P3 claim + pre-check heal that
        window. failed => typed error (the caller asked for proof);
        unknown-only => success with the packages unrecorded (re-install
        converges: late-record)."""
        from .spec import parse_cran_dep, split_constraint
        from .verify import default_checks, explicit_checks, usable_want
        s = self._get(session_id)
        requested = {"conda": conda, "pypi": pypi, "cran": cran}
        lanes: dict[str, tuple[list, dict, dict]] = {}
        for eco in ("conda", "pypi"):
            names, pins, by_name = [], {}, {}
            for e in requested[eco]:
                n, c = split_constraint(e)
                names.append(n)
                by_name[n] = e
                if usable_want(c):
                    pins[n] = c.strip()
            lanes[eco] = (names, pins, by_name)
        names, pins, by_name = [], {}, {}
        refs = []
        for e in requested["cran"]:
            p = parse_cran_dep(e)
            if p["kind"] == "github":
                refs.append(e)
                continue
            names.append(p["name"])
            by_name[p["name"]] = e
            if p["version"]:
                pins[p["name"]] = f'=={p["version"]}'
        resolved = list(out.get("resolved") or [])
        if refs and len(resolved) == len(refs):
            for rname, rentry in zip(resolved, refs):
                names.append(rname)
                by_name[rname] = rentry     # verify keys on RESOLVED name
        else:
            names += resolved               # coarse: any ref failure
            for rname in resolved:          # retracts all refs below
                by_name[rname] = None
        lanes["cran"] = (names, pins, by_name)

        if eff:
            lane_of = {n: "cran" for n in lanes["cran"][0]}
            lane_of.update({n: "conda" for n in lanes["conda"][0]})
            checks = explicit_checks(eff, lane_of)
        else:
            checks = []
            for eco in ("conda", "pypi", "cran"):
                nm, pn, _ = lanes[eco]
                checks += default_checks(eco, nm, pn)
        from .verify import run_grouped
        fn = self._verify_exec_fn(s, adapter)
        verified = run_grouped(fn, checks)

        not_passed = {n for n, r in verified.items()
                      if r["status"] != "passed"}
        retract: dict[str, list] = {"conda": [], "pypi": [], "cran": []}
        for eco in ("conda", "pypi", "cran"):
            _, _, by_name = lanes[eco]
            for n in not_passed:
                if n in by_name:
                    e = by_name[n]
                    if e is None:           # coarse ref mapping
                        retract[eco] = [x for x in requested[eco]
                                        if "/" in x.partition("@")[0]]
                    elif e not in retract[eco]:
                        retract[eco].append(e)
        if any(retract.values()):
            self.store.session_remove_deps(session_id, retract["conda"],
                                           retract["pypi"],
                                           retract["cran"])
        self.store.emit(
            "session.verified", session=session_id,
            passed=sorted(n for n, r in verified.items()
                          if r["status"] == "passed"),
            failed=sorted(n for n, r in verified.items()
                          if r["status"] == "failed"),
            unknown=sorted(n for n, r in verified.items()
                           if r["status"] == "unknown"))
        failed = {n: r for n, r in verified.items()
                  if r["status"] == "failed"}
        if failed:
            raise WeftError(
                "env.realize_failed",
                f"postcondition failed for {sorted(failed)} — installed "
                f"but not proven (wrong version, wrong package, or "
                f"broken load)",
                stage="realize",
                hints={"postcondition": True, "verified": verified,
                       "retracted": {k: v for k, v in retract.items()
                                     if v},
                       "runtime": out.get("runtime"),
                       "note": "retracted entries are NOT in the "
                               "session record; the disk residue is "
                               "scratch and a snapshot will not carry "
                               "it"})
        out["verified"] = verified
        unknown = [n for n, r in verified.items()
                   if r["status"] == "unknown"]
        if unknown:
            out["unverified_note"] = (
                f"{sorted(unknown)} could not be verified (oracle could "
                f"not run) and are NOT recorded — re-running the install "
                f"converges once verification succeeds (late-record)")
        return out

    def _base_cold(self, s: dict, adapter: SiteAdapter) -> bool:
        """Is the base's package cache COLD on this site? An adopted
        (read-only) or archive-unpacked (packed) realization was never
        BUILT here, so the site's pixi cache holds none of its packages —
        cloning the manifest would re-download the entire base from the
        index (1.6 GB in the field report; impossible on an
        egress-restricted node). A locally built env — including a
        locally built squashfs — populated the cache and clones cheap."""
        real = self.store.get_realization(s["base_env_id"], adapter.name)
        if not real:
            return False
        return bool(real.get("read_only")) \
            or "packed" in (real.get("strategy") or "")

    def _cold_refusal(self, s: dict, what: str) -> WeftError:
        env_row = self.store.get_env(s["base_env_id"]) or {}
        return WeftError(
            "session.cold_base",
            f"{what} needs a writable clone of the base, but the base was "
            "adopted/unpacked on this site (cold package cache) — cloning "
            "re-downloads the ENTIRE base from the index",
            stage="realize",
            hints={"delta_lanes": {
                "pypi": "session_install(pypi=[...]) layers delta-only "
                        "over this base (pylib)",
                "cran": "session_install(cran=[...]) layers delta-only "
                        "over this base (rlib) — plain names, "
                        "'name ==X.Y.Z', AND 'owner/repo@ref' github "
                        "sources; cran_repos=[url] adds repositories",
                "installer": "session_run_installer(cmd, "
                             "writes_to='rlib'|'pylib') declares the "
                             "write target as the session layer and runs "
                             "over the read-only base",
                "conda": "cannot layer (embedded prefixes) — use a lever "
                         "below",
                "julia": "not yet wired for sessions; extends_env works",
            }, "options": {
                "extends": "mint a real delta env instead: env_ensure("
                           f"{{'extends': '{env_row.get('spec_hash', '?')}',"
                           " 'deps': {...}}) — pypi/cran/julia deltas "
                           "realize as overlays; a conda delta is a full "
                           "realize",
                "warm_site": "run this where the base was BUILT (warm pixi "
                             "cache): the clone is a local hardlink forest "
                             "there",
                "full_clone": "pass full_clone=true to fetch the whole base "
                              "from the index here (needs egress and time)",
            }})

    def _materialize_pylib(self, s: dict, adapter: SiteAdapter,
                           pypi: list[str]) -> dict:
        """The cold-base pypi lane: install ONLY what the base lacks into
        a session-owned pylib layer, composed over the mounted base via
        PYTHONPATH/PATH — no clone, no base re-download.

        pip's --target resolution IGNORES the running env (verified: a
        satisfied dep is re-downloaded into the target), so this is
        two-phase: (A) `pip install --dry-run --report` UNDER base
        activation (+ the existing pylib layer) — dry-run respects the
        active env, so the report is exactly the missing closure at
        resolved pins; (B) `pip install --no-deps --target` for that
        set. Old pip without --report falls back to the full-closure
        --target install — correct, fatter, and SAID."""
        import json as _json
        import time as _t
        t_start = _t.monotonic()
        act, ns = self._base_activation(s, adapter)
        sdir = adapter.path(s["location"])
        pylib = f"{sdir}/pylib"
        overlay_rel = f"{s['location']}/overlay.sh"
        overlay = f"{sdir}/overlay.sh"
        from .realize import _ns_wrap_cmd
        wrap = _ns_wrap_cmd if ns else (lambda x: x)
        specs = " ".join(shlex.quote(_pixi_spec(p)) for p in pypi)
        report_rel = f"{s['location']}/pip-report.json"
        pre = (f"{act} && "
               f"{{ [ -f {shlex.quote(overlay)} ] && "
               f". {shlex.quote(overlay)}; true; }} && ")
        note = None
        resolve_s = fetch_s = 0.0
        fetch_method = None
        t0 = _t.monotonic()
        try:
            ra = adapter.run_activated(wrap(
                pre + f"python -m pip install --dry-run --quiet --report "
                      f"{shlex.quote(adapter.path(report_rel))} {specs}"),
                timeout=600)
        except WeftError as e:
            raise WeftError(
                "env.realize_failed",
                "resolving the pypi delta stalled or timed out",
                stage="realize", retryable=True,
                hints={"likely": "stalled index/CDN transfer from this "
                                 "node", "detail": e.detail}) from e
        resolve_s = round(_t.monotonic() - t0, 2)
        missing: list[str] = []
        if ra.rc != 0 and ("no such option" in (ra.err + ra.out)
                           or "unrecognized arguments" in (ra.err + ra.out)):
            r = adapter.run_activated(wrap(
                f"{act} && mkdir -p {shlex.quote(pylib)} && "
                f"python -m pip install --no-input --quiet "
                f"--target {shlex.quote(pylib)} {specs}"), timeout=1800)
            if r.rc != 0:
                tail = (r.err or r.out)[-1500:]
                code, retryable = _pip_failure(tail)
                raise WeftError(
                    code, "pypi install into the session layer failed",
                    stage="realize", retryable=retryable,
                    hints={"log_tail": tail})
            note = ("this site's pip predates --dry-run/--report: the "
                    "full dependency closure was installed into the "
                    "session layer (base-satisfied deps duplicated)")
        elif ra.rc != 0:
            # resolution ran and failed: a real conflict says so in the
            # log; an unrecognized crash is solver INFRASTRUCTURE, not
            # proof the spec is unsatisfiable
            tail = (ra.err or ra.out)[-1500:]
            code, retryable = _pip_failure(
                tail, default="env.solve_failed", default_retryable=True)
            raise WeftError(
                code, "pypi delta resolution failed against the base",
                stage="realize", retryable=retryable,
                hints={"requested": pypi, "log_tail": tail})
        else:
            data = _json.loads(adapter.read_file(report_rel).decode())
            missing = [f'{i["metadata"]["name"]}=={i["metadata"]["version"]}'
                       for i in data.get("install", [])]
            if missing:
                pins = " ".join(shlex.quote(m) for m in missing)
                # phase B is --no-deps at exact pins: uv (when the site
                # has it) is much faster at wheel fetch/extract; pip is
                # the always-there fallback — the marker says which ran
                fetch_cmd = (
                    f"{act} && mkdir -p {shlex.quote(pylib)} && "
                    f"if command -v uv >/dev/null 2>&1 && "
                    f"uv pip install --no-deps --target {shlex.quote(pylib)} "
                    f"--python \"$(command -v python)\" {pins} "
                    f">/dev/null 2>&1; then echo '#fetch uv'; "
                    f"else python -m pip install --no-deps --no-input "
                    f"--quiet --target {shlex.quote(pylib)} {pins} "
                    f"&& echo '#fetch pip'; fi")
                t1 = _t.monotonic()
                try:
                    rb = adapter.run_activated(wrap(fetch_cmd),
                                               timeout=1800)
                except WeftError as e:
                    raise WeftError(
                        "env.realize_failed",
                        "fetching the pypi delta stalled or timed out",
                        stage="realize", retryable=True,
                        hints={"missing": missing,
                               "detail": e.detail}) from e
                if rb.rc != 0:
                    # phase B is --no-deps at exact pins: by construction
                    # NO resolution happens here — "unsatisfiable spec"
                    # was always the wrong verdict for this raise
                    tail = (rb.err or rb.out)[-1500:]
                    code, retryable = _pip_failure(tail)
                    raise WeftError(
                        code, "installing the pypi delta failed",
                        stage="realize", retryable=retryable,
                        hints={"missing": missing, "log_tail": tail})
                fetch_s = round(_t.monotonic() - t1, 2)
                fetch_method = "uv" if "#fetch uv" in rb.out else "pip"
            else:
                note = "already satisfied by the base — nothing fetched"
        # ADDITIVE: overlay.sh is shared with the rlib layer — never clobber
        self._ensure_overlay_line(
            s, adapter,
            f'export PYTHONPATH="{pylib}' + '${PYTHONPATH:+:$PYTHONPATH}"')
        self._ensure_overlay_line(
            s, adapter, f'export PATH="{pylib}/bin:$PATH"')
        first = s.get("materialize_mode", "clone") != "pylib"
        if first:
            self.store.set_session_materialized(s["session_id"],
                                                mode="pylib")
            s["materialized"], s["materialize_mode"] = True, "pylib"
            self.store.emit("session.materialized",
                            session=s["session_id"],
                            base=s["base_env_id"], mode="pylib")
        # shadowing is tolerable in scratch, but SAY it: a pylib copy of
        # a base-held name wins on sys.path
        env_row = self.store.get_env(s["base_env_id"]) or {}
        base_pypi = {p["name"].lower().replace("_", "-")
                     for plat in (env_row.get("canonical") or {})
                     .get("platforms", {}).values()
                     for p in plat if p["kind"] == "pypi"}
        shadows = [m.split("==")[0] for m in missing
                   if m.split("==")[0].lower().replace("_", "-")
                   in base_pypi]
        out = {"mode": "pylib", "fetched": missing, "first": first,
               "timings": {"resolve_s": resolve_s, "fetch_s": fetch_s,
                           "total_s": round(_t.monotonic() - t_start, 2)}}
        if fetch_method:
            out["fetch_method"] = fetch_method
        if note:
            out["note"] = note
        if shadows:
            out["shadows_base"] = shadows
        return out

    def _get(self, session_id: str, allow_stopped: bool = False) -> dict:
        s = self.store.get_session(session_id)
        if not s or (s["state"] != "active" and not allow_stopped):
            raise WeftError(
                "task.invalid", f"no active session {session_id}", stage="infra",
            )
        if s["state"] == "active":
            # every session verb passes through here — last_used is the
            # activity fact idle policies and evict hints reason from
            self.store.touch_session(session_id)
        return s

    def _composed(self, s: dict, adapter: SiteAdapter) -> tuple[str, bool]:
        """The ONE composition rule for every consumer (exec, kernels
        via driver, the verify oracle): interpreter-stack activation,
        then overlay.sh when any layer exists (pylib PYTHONPATH, rlib
        R_LIBS). The verify oracle MUST run through this — anything
        else verifies a different world than user code sees."""
        main, ns = self._stack_activation(s, adapter)
        overlay = adapter.path(f"{s['location']}/overlay.sh")
        return (f"{main} && {{ [ -f {shlex.quote(overlay)} ] && "
                f". {shlex.quote(overlay)}; true; }}"), ns

    def _verify_exec_fn(self, s: dict, adapter: SiteAdapter):
        pre, ns = self._composed(s, adapter)

        def run(script: str, timeout: float):
            full = f"{pre} && ( {script} )"
            if ns:
                from .realize import _ns_wrap_cmd
                full = _ns_wrap_cmd(full)
            return adapter.run_cmd(full, timeout=timeout)

        return run

    def exec(self, session_id: str, adapter: SiteAdapter, cmd: str) -> dict:
        s = self._get(session_id)
        sdir = shlex.quote(adapter.path(s["location"]))
        pre, ns = self._composed(s, adapter)
        script = f"cd {sdir} && {pre} && ( {cmd} )"
        if ns:
            from .realize import _ns_wrap_cmd
            script = _ns_wrap_cmd(script)
        r = adapter.run_cmd(script, timeout=600)
        self.store.audit_log(None, "session.exec", site=adapter.name,
                             command=cmd, why=f"session {session_id}",
                             result=f"rc={r.rc}")
        return {"rc": r.rc, "stdout": r.out[-8000:], "stderr": r.err[-4000:]}

    def install(self, session_id: str, adapter: SiteAdapter,
                conda: list[str] | None = None,
                pypi: list[str] | None = None, fast: bool = True,
                full_clone: bool = False,
                cran: list[str] | None = None,
                cran_repos: list[str] | None = None,
                verify=None) -> dict:
        """Public entry: the install flow, then (when verify= is on)
        the POSTCONDITION phase in the composed runtime, with record-
        gating — records exist exactly when verification passed
        (ensure_available P1). verify=None/False is byte-identical to
        the pre-verify contract."""
        eff = None
        if verify not in (None, False):
            from .verify import validate_verify
            eff = validate_verify(verify)
        out = self._install_inner(session_id, adapter, conda=conda,
                                  pypi=pypi, fast=fast,
                                  full_clone=full_clone, cran=cran,
                                  cran_repos=cran_repos)
        if eff is None:
            return out
        # postconditions key on the ORIGINAL request entries — the
        # result's installed lists are normalized (pins stripped), and
        # the records to gate hold the original strings
        return self._postconditions(session_id, adapter, out, eff,
                                    conda=list(conda or []),
                                    pypi=list(pypi or []),
                                    cran=list(cran or []))

    def _install_inner(self, session_id: str, adapter: SiteAdapter,
                       conda: list[str] | None = None,
                       pypi: list[str] | None = None, fast: bool = True,
                       full_clone: bool = False,
                       cran: list[str] | None = None,
                       cran_repos: list[str] | None = None) -> dict:
        """Add packages to the session. pypi-only requests take a FAST
        PATH by default: a direct pip/uv install into the session prefix
        — no re-solve of the whole manifest (which dominates a one-leaf
        add on a big base). The package is still recorded as a DEP, so
        the snapshot's full re-solve remains the identity mint and the
        conflict check; the session itself is unhashed scratch by
        contract, so in-prefix divergence is tolerable until then.
        fast=False (or any conda dep) keeps solve-at-add; a failed fast
        install falls through to the full path automatically."""
        s = self._get(session_id)
        conda, pypi = list(conda or []), list(pypi or [])
        cran = list(cran or [])
        if not conda and not pypi and not cran:
            raise WeftError("task.invalid", "nothing to install", stage="realize")
        # same intake contract as env specs: a name twice in ONE call is
        # a malformed request, refused before any tool sees it (each
        # ecosystem otherwise fails its own way — R tolerates silently,
        # pip errors in its own words). Re-adding across CALLS stays fine.
        from .spec import refuse_duplicate_deps
        refuse_duplicate_deps("conda", conda, where="install")
        refuse_duplicate_deps("pypi", pypi, where="install")
        refuse_duplicate_deps("cran", cran, where="install")
        # cran is ORTHOGONAL to the prefix modes: R composes via
        # R_LIBS on any base (frozen or built-here), so it never clones,
        # never flips the session's mode, and works the same everywhere
        rlib_out = None
        if cran:
            rlib_out = self._materialize_rlib(s, adapter, cran,
                                              extra_repos=cran_repos)
            self.store.session_add_deps(session_id, [], [], cran,
                                        cran_repos=cran_repos)
            # keep the in-memory row honest: runtime() below reads it
            s["added_cran"] = s.get("added_cran", []) + cran
            if not conda and not pypi:
                return {"installed": {"conda": [], "pypi": [],
                                      "cran": rlib_out["installed"]},
                        "session_id": session_id, "mode": "rlib",
                        "rlib": rlib_out["rlib"],
                        **({"resolved": rlib_out["resolved"]}
                           if rlib_out.get("resolved") else {}),
                        "runtime": self.runtime(s, adapter),
                        "note": "R delta composed over the base via "
                                "R_LIBS — dependencies the base holds "
                                "were skipped natively; the snapshot's "
                                "solve pins versions"}
        # COLD base (adopted/unpacked here — empty package cache): a full
        # clone re-downloads the entire base, so pypi adds go into a
        # pylib overlay over the mount and conda adds refuse with levers.
        mode = s.get("materialize_mode",
                     "clone" if s.get("materialized", True) else "none")
        if mode != "clone" and not full_clone \
                and self._base_cold(s, adapter):
            if conda:
                raise self._cold_refusal(
                    s, f"adding conda package(s) {conda}")
            got = self._materialize_pylib(s, adapter, pypi)
            self.store.session_add_deps(session_id, [], pypi)
            self.store.emit("session.installed", session=session_id,
                            conda=[], pypi=pypi, mode="pylib",
                            **got.get("timings", {}))
            out = {"installed": {"conda": [], "pypi": pypi},
                   "session_id": session_id, "mode": "pylib",
                   "fetched": got["fetched"],
                   "timings": got.get("timings"),
                   "fetch_method": got.get("fetch_method"),
                   "runtime": self.runtime(s, adapter),
                   "note": got.get("note") or
                           "only the missing closure was fetched; the "
                           "base stays on its mount — the snapshot's "
                           "full re-solve remains the conflict check"}
            if got.get("shadows_base"):
                out["shadows_base"] = got["shadows_base"]
            if got.get("first"):
                out["materialized_note"] = (
                    "pylib overlay created over the base; running python "
                    "kernels see the new packages on their next block")
            if rlib_out:
                out["installed"]["cran"] = rlib_out["installed"]
                out["rlib"] = rlib_out["rlib"]
            return out
        if mode == "pylib" and full_clone:
            raise WeftError(
                "task.invalid",
                "this session already materialized as a pylib overlay — "
                "snapshot it to mint a real env instead of mixing modes",
                stage="realize")
        # first mutation pays for mutability: clone the prefix now
        first_clone = self._materialize(s, adapter)
        clone_note = ("writable prefix cloned on this first install; "
                      "running python kernels see the new packages on "
                      "their next block (forward hook); R/julia kernels "
                      "attached before this need kernel_restart"
                      ) if first_clone else None
        fallback_tail = None
        if fast and pypi and not conda:
            out = self._fast_pypi(s, adapter, pypi)
            if out is not None and "error" not in out:
                self.store.session_add_deps(session_id, [], pypi)
                self.store.emit("session.installed", session=session_id,
                                conda=[], pypi=pypi, fast=True)
                fast_out = {"installed": {"conda": [], "pypi": pypi},
                            "session_id": session_id, "solved": False,
                            "method": out["method"],
                            "verified_at": "snapshot",
                            "runtime": self.runtime(s, adapter),
                            "note": "installed without a solve — the "
                                    "snapshot's full re-solve is the "
                                    "conflict check; pass fast=False to "
                                    "solve at add time"}
                if clone_note:
                    fast_out["materialized_note"] = clone_note
                if rlib_out:
                    fast_out["installed"]["cran"] = rlib_out["installed"]
                    fast_out["rlib"] = rlib_out["rlib"]
                return fast_out
            fallback_tail = (out or {}).get("detail")
        manifest = adapter.path(f"{s['location']}/pixi.toml")
        parts = []
        if conda:
            parts.append(
                f"{shlex.quote(adapter.pixi_bin)} add --manifest-path "
                f"{shlex.quote(manifest)} {' '.join(shlex.quote(_pixi_spec(c)) for c in conda)}"
            )
        if pypi:
            parts.append(
                f"{shlex.quote(adapter.pixi_bin)} add --pypi --manifest-path "
                f"{shlex.quote(manifest)} {' '.join(shlex.quote(_pixi_spec(p)) for p in pypi)}"
            )
        r = adapter.run_cmd(" && ".join(parts), timeout=900)
        if r.rc != 0:
            tail = (r.err or r.out)[-1500:]
            code, retryable, why, stg = _pixi_add_failure(tail)
            hints = {"log_tail": tail, "requested": conda + pypi}
            if code == "internal.error":
                hints["suggestion"] = ("the session manifest is corrupt "
                                       "(crashed earlier add?) — snapshot "
                                       "what you can and start a fresh "
                                       "session")
            raise WeftError(code, why, stage=stg, retryable=retryable,
                            hints=hints)
        self.store.session_add_deps(session_id, conda, pypi)
        self.store.emit("session.installed", session=session_id,
                        conda=conda, pypi=pypi)
        out = {"installed": {"conda": conda, "pypi": pypi},
               "session_id": session_id,
               # the flip moment: a caller holding start-time runtime
               # would exec the wrong thing from here on — hand it the
               # fresh contract at exactly the call that changed it
               "runtime": self.runtime(s, adapter)}
        if rlib_out:
            out["installed"]["cran"] = rlib_out["installed"]
            out["rlib"] = rlib_out["rlib"]
        if clone_note:
            out["materialized_note"] = clone_note
        if fallback_tail:
            out["fast_fallback"] = ("direct install failed; solved the "
                                    "full manifest instead: "
                                    + fallback_tail[-300:])
        return out

    def _fast_pypi(self, s: dict, adapter: SiteAdapter,
                   pypi: list[str]) -> dict | None:
        """Direct install into the session prefix — uv when the site has
        it (much faster), else the prefix's own pip. Returns None when
        neither tool exists (silent fall-through to the solve path);
        {"error", "detail"} when the install itself failed (reported on
        the fallback result — a real conflict is worth seeing)."""
        py = adapter.path(
            f"{s['location']}/.pixi/envs/default/bin/python")
        pkgs = " ".join(shlex.quote(_pixi_spec(p)) for p in pypi)
        r = adapter.run_cmd(
            f"if command -v uv >/dev/null 2>&1; then echo '#method uv'; "
            f"uv pip install --python {shlex.quote(py)} {pkgs}; "
            f"elif {shlex.quote(py)} -m pip --version >/dev/null 2>&1; "
            f"then echo '#method pip'; "
            f"{shlex.quote(py)} -m pip install --quiet {pkgs}; "
            f"else echo '#method none'; exit 87; fi",
            timeout=600)
        method = "uv" if "#method uv" in r.out else \
            "pip" if "#method pip" in r.out else None
        if method is None:
            return None                    # no tool: not an error
        if r.rc != 0:
            tail = (r.err or r.out)[-1500:]
            return {"error": _pip_failure(tail)[0], "detail": tail}
        return {"method": method}

    def run_installer(self, session_id: str, adapter: SiteAdapter, cmd: str,
                      note: str = "", source: str | None = None,
                      full_clone: bool = False,
                      writes_to: str | None = None,
                      verify=None) -> dict:
        """The bespoke install that no index can express — an R
        install.packages, a pip install -e, a vendored make install. A
        normal, supported move: it runs in the session AND is captured, so
        `snapshot` can carry it into the spec as a labeled post_install step.

        `source` is a local path (a source tree, a wheel) the command needs:
        weft content-addresses it so the step travels with the env and
        rebuilds ANYWHERE. Without it, a step that reads local paths mints
        an env only this machine can build — the grade will say so."""
        s = self._get(session_id)
        # an UNDECLARED installer mutates the prefix arbitrarily — it
        # needs a real writable clone; on a cold base that means
        # re-downloading the base. writes_to DECLARES the write target
        # as the session's own layer: the base is filesystem-read-only
        # anyway (EROFS), weft provisions the layer and points the
        # ecosystem's env at it, and the command runs over the mount.
        layer_run = False
        if not full_clone and self._base_cold(s, adapter) \
                and s.get("materialize_mode", "clone") != "clone":
            if writes_to not in ("rlib", "pylib"):
                raise self._cold_refusal(s, "an undeclared installer")
            layer_run = True
        first_clone = False
        if not layer_run:
            # an installer mutates the prefix: first mutation clones it
            first_clone = self._materialize(s, adapter)
        captured = None
        if source:
            from pathlib import Path as _P
            info = self.dataman.register(_P(source).resolve())
            mount = _P(source).name
            captured = {"ref": info["ref"], "mount_as": mount}
            # stage it into the session too, so the same command line works
            # here and at realization (relative paths resolve identically)
            self._stage(adapter, s["location"], captured)
        if layer_run:
            act, ns = self._stack_activation(s, adapter)
            sdir = adapter.path(s["location"])
            layer = f"{sdir}/{writes_to}"
            if writes_to == "rlib":
                self._ensure_overlay_line(
                    s, adapter,
                    f'export R_LIBS="{layer}' + '${R_LIBS:+:$R_LIBS}"')
                point = f'export R_LIBS={shlex.quote(layer)}"${{R_LIBS:+:$R_LIBS}}"'
            else:
                self._ensure_overlay_line(
                    s, adapter,
                    f'export PYTHONPATH="{layer}'
                    + '${PYTHONPATH:+:$PYTHONPATH}"')
                point = (f'export PIP_TARGET={shlex.quote(layer)} && '
                         f'export PYTHONPATH={shlex.quote(layer)}'
                         '"${PYTHONPATH:+:$PYTHONPATH}"')
            overlay = f"{sdir}/overlay.sh"
            script = (f"cd {shlex.quote(sdir)} && {act} && "
                      f"{{ [ -f {shlex.quote(overlay)} ] && "
                      f". {shlex.quote(overlay)}; true; }} && "
                      f"mkdir -p {shlex.quote(layer)} && {point} && "
                      f"( {cmd} )")
            if ns:
                from .realize import _ns_wrap_cmd
                script = _ns_wrap_cmd(script)
            r = adapter.run_activated(script, timeout=3600)
        else:
            manifest = adapter.path(f"{s['location']}/pixi.toml")
            r = adapter.run_activated(
                f"cd {shlex.quote(adapter.path(s['location']))} && "
                f"eval \"$({shlex.quote(adapter.pixi_bin)} shell-hook "
                f"--manifest-path {shlex.quote(manifest)})\" && ( {cmd} )",
                timeout=3600)
        if r.rc != 0:
            raise WeftError(
                "env.realize_failed",
                "session installer failed",
                stage="realize",
                hints={"command": cmd, "log_tail": (r.err or r.out)[-1500:]})
        self.store.session_add_installer(session_id, cmd, note,
                                         input=captured)
        self.store.emit("session.installer", session=session_id, cmd=cmd[:200],
                        portable=bool(captured))
        out = {"session_id": session_id, "installed": cmd,
               "captured": True, "portable": bool(captured),
               "source_ref": (captured or {}).get("ref"),
               "note": "snapshot will carry this as a post_install step "
                       "(grade: escape-hatch) with your note attached"
                       + ("; its source travels with the env, so it rebuilds "
                          "anywhere" if captured else
                          " — pass source=<path> if the command needs local "
                          "files, or the env will only rebuild on this "
                          "machine")}
        out["runtime"] = self.runtime(s, adapter)
        if layer_run:
            out["writes_to"] = writes_to
            out["note"] += (
                "; DECLARED-TARGET run on the read-only base: the command "
                "wrote into the session layer only. Snapshot honesty: a "
                "spec carrying post_install steps realizes FULL (extras "
                "deltas cannot overlay) — prefer session_install(cran=/"
                "pypi=) when the addition fits, to keep the snapshot "
                "O(delta) on this base")
        if first_clone:
            out["materialized_note"] = (
                "writable prefix cloned on this first installer; running "
                "python kernels see its packages on their next block "
                "(forward hook); R/julia kernels attached before this "
                "need kernel_restart")
        if verify not in (None, False):
            # a bespoke cmd has no derivable defaults — weft cannot know
            # what it was supposed to produce; EXPLICIT checks only
            from .verify import (explicit_checks, run_grouped,
                                 validate_verify)
            eff = validate_verify(verify)
            if not eff:
                raise WeftError(
                    "task.invalid",
                    "run_installer verify must be explicit — weft "
                    "cannot derive a postcondition from an arbitrary "
                    "command",
                    stage="realize",
                    hints={"example": {"loads": ["<pkg>"],
                                       "import": ["<module>"],
                                       "versions": {"<name>": ">=1.0"}}})
            lane_of = {n: "cran" for n in eff.get("loads", [])}
            checks = explicit_checks(eff, lane_of)
            fn = self._verify_exec_fn(s, adapter)
            verified = run_grouped(fn, checks)
            out["verified"] = verified
            failed = {n: r for n, r in verified.items()
                      if r["status"] == "failed"}
            self.store.emit("session.verified", session=session_id,
                            passed=sorted(n for n, r in verified.items()
                                          if r["status"] == "passed"),
                            failed=sorted(failed),
                            unknown=sorted(
                                n for n, r in verified.items()
                                if r["status"] == "unknown"))
            if failed:
                raise WeftError(
                    "env.realize_failed",
                    f"installer postcondition failed for "
                    f"{sorted(failed)}",
                    stage="realize",
                    hints={"postcondition": True, "verified": verified,
                           "runtime": out.get("runtime"),
                           "note": "the installer step IS captured "
                                   "(snapshot carries it); the "
                                   "postcondition proves it did not "
                                   "produce what you asserted"})
        return out

    def snapshot(self, session_id: str, name: str | None = None,
                 notes: list[str] | None = None, verify: bool = True) -> dict:
        """Synthesize the spec delta, re-solve properly, return a real EnvID.

        Captured installers ride along as labeled post_install steps, with
        their captured sources as content-addressed `post_install_inputs` —
        so the escape hatch is PORTABLE (it rebuilds where the session's
        filesystem does not exist). `verify` then actually *realizes* the
        minted env before handing it back: a "citable EnvID" that cannot be
        rebuilt is worse than an error (live-agent eval finding)."""
        # snapshot synthesizes from the RECORD (base + captured
        # installs) — the live prefix is not needed, so a STOPPED
        # session still snapshots (late saves from session kernels;
        # retention.md R6)
        s = self._get(session_id, allow_stopped=True)
        if not (s["added_conda"] or s["added_pypi"]
                or s.get("added_cran") or s.get("installers")):
            # nothing was added: the citable env IS the base. Re-solving
            # a zero-delta spec at snapshot time could pick newer builds
            # and mint a DIFFERENT EnvID for identical intent — identity
            # must not drift with the date the user pressed snapshot.
            self.store.emit("session.snapshot", session=session_id,
                            env_id=s["base_env_id"])
            return {"env_id": s["base_env_id"], "session_id": session_id,
                    "note": "session added nothing — the base env is "
                            "the snapshot"}
        env_row = self.store.get_env(s["base_env_id"])
        installers = s.get("installers") or []
        spec = {
            "name": name or f"snapshot-of-{session_id}",
            "extends": env_row["spec_hash"],
            "deps": {"conda": s["added_conda"], "pypi": s["added_pypi"]},
        }
        if s.get("added_cran"):
            # the spec's native cran layer: the solve pins versions the
            # scratch install left floating (github refs get SHA-pinned);
            # classify_delta layers cran, so this realizes as a delta
            # overlay on the frozen base
            spec["deps"]["cran"] = s["added_cran"]
        if s.get("added_cran_repos"):
            spec["r_repositories"] = s["added_cran_repos"]
        if installers:
            spec["post_install"] = [i["cmd"] for i in installers]
            spec["step_notes"] = {str(i): inst["note"]
                                  for i, inst in enumerate(installers)
                                  if inst.get("note")}
            inputs = [i["input"] for i in installers if i.get("input")]
            if inputs:
                spec["post_install_inputs"] = inputs
        if notes:
            spec["notes"] = list(notes)
        result = self.envman.ensure(spec)
        out = {**result, "spec": spec,
               "note": "re-run the final computation under this EnvID to "
                       "enter it into provenance"}
        if installers:
            out["carried_installers"] = len(installers)

        # lint: a step that names a path only THIS machine has will rebuild
        # here and nowhere else — verification can't see that (it succeeds
        # locally), so say it plainly. Inform, don't scold.
        unportable = _unportable_paths(installers)
        if unportable:
            out["portability_warning"] = {
                "paths": unportable,
                "detail": "these installer steps reference local paths that "
                          "are not part of the env: it will rebuild on this "
                          "machine and fail elsewhere",
                "fix": "re-run the installer with "
                       "session_run_installer(..., source=<path>) so weft "
                       "content-addresses the sources into the env",
            }
            self.store.emit("session.snapshot_unportable",
                            session=session_id, paths=unportable)

        if verify and installers:
            adapter = self._adapters.get(s["site"]) if self._adapters else None
            try:
                self._verify(result["env_id"], adapter)
                out["verified"] = True
            except WeftError as e:
                out = e.to_dict()
                out["env_id"] = result["env_id"]
                out["detail"] = (
                    "the snapshot env was minted but does NOT rebuild: " +
                    e.detail)
                out["hints"] = {
                    **e.hints,
                    "suggestion": "an installer step depends on something "
                                  "that is not in the env: register its "
                                  "sources (data_register) and re-run it via "
                                  "session_run_installer(..., inputs=[...]), "
                                  "or make the step self-contained",
                }
                self.store.emit("session.snapshot_unverified",
                                session=session_id, env_id=result["env_id"])
                return out
        self.store.emit("session.snapshot", session=session_id,
                        env_id=result["env_id"])
        return out

    def _stage(self, adapter: SiteAdapter, location: str, entry: dict) -> None:
        from .task import Task
        t = Task.from_dict({"command": "true",
                            "inputs": [{"ref": entry["ref"],
                                        "mount_as": entry["mount_as"]}]})
        self.dataman.ensure_at([entry["ref"]], adapter,
                               self.runner.transfers)
        plan = self.dataman.materialize_plan(t, site=adapter.name)
        adapter.write_file(f"{location}/inputs.tsv", plan.encode())
        endpoint = adapter.transfer_endpoint()
        r = adapter.shim(
            ["materialize", "--cas", endpoint["cas_root"],
             "--dir", adapter.path(location),
             "--plan", adapter.path(f"{location}/inputs.tsv")], timeout=600)
        if r.rc != 0:
            raise WeftError("env.realize_failed",
                            "could not stage the installer's source",
                            stage="realize", hints={"detail": r.err[:300]})

    def _verify(self, env_id: str, adapter) -> None:
        """Realize the minted env from scratch — the only honest proof."""
        if adapter is None or self.runner is None:
            return
        from .realize import ensure_realization, env_dir_rel, _wipe_aside
        rel = env_dir_rel(env_id)
        _wipe_aside(adapter, rel, recreate=False)
        self.store.set_realization(env_id, adapter.name, "prefix", rel,
                                   "missing")
        site_row = self.store.get_site(adapter.name) or {}
        env_row = self.store.get_env(env_id)
        ensure_realization(
            env_id, env_row, adapter, self.store,
            caps=site_row.get("capabilities"),
            site_config=site_row.get("config"),
            pack_tools={"pixi_pack": self.runner.pixi_pack,
                        "cas": self.runner.cas,
                        "transfers": self.runner.transfers,
                        "solvers": self.envman.solvers,
                        "store": self.store,
                        "dataman": self.runner.dataman})

    def stop(self, session_id: str, adapter: SiteAdapter) -> dict:
        s = self._get(session_id)
        # rename-aside + background unlink: a materialized prefix is a
        # ~10^5-file tree — synchronous rm gated stop for minutes on
        # parallel filesystems
        from .realize import _wipe_aside
        _wipe_aside(adapter, s["location"], recreate=False)
        self.store.set_session_state(session_id, "stopped")
        self.store.emit("session.stopped", session=session_id)
        return {"session_id": session_id, "state": "stopped"}


def _pixi_spec(dep: str) -> str:
    """'numpy >=2' -> 'numpy>=2' (pixi add wants no space). Soft-pin
    '?' is WEFT vocabulary, not pip/uv/pixi vocabulary — leaked, it
    reached the tools as a literal and died as 'Invalid requirement'
    (2026-07 vocabulary sweep #3)."""
    from .spec import strip_soft
    return strip_soft(dep).replace(" ", "")


def _unportable_paths(installers: list[dict]) -> list[str]:
    """Tokens in an installer command that name a filesystem path the env
    does not carry — the difference between an escape hatch that travels
    and one that quietly depends on one filesystem. Path-SHAPED is enough:
    the session ran on a site whose filesystem this controller cannot
    stat, so existence here proves nothing either way."""
    out = []
    for inst in installers:
        if inst.get("input"):
            continue          # its source travels with the env
        for token in inst["cmd"].split():
            token = token.strip("'\"")
            if token.startswith(("-", "http://", "https://", "git+")):
                continue
            if token.startswith(("/", "./", "../", "~")):
                out.append(token)
    return out
