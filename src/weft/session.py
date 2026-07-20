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

import shlex
import uuid

from .adapters.base import SiteAdapter
from .envman import EnvManager
from .errors import WeftError
from .realize import env_dir_rel
from .store import Store


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

    def exec(self, session_id: str, adapter: SiteAdapter, cmd: str) -> dict:
        s = self._get(session_id)
        sdir = shlex.quote(adapter.path(s["location"]))
        if s.get("materialized", True):
            manifest = adapter.path(f"{s['location']}/pixi.toml")
            script = (f"cd {sdir} && "
                      f"eval \"$({shlex.quote(adapter.pixi_bin)} shell-hook "
                      f"--manifest-path {shlex.quote(manifest)})\" && ( {cmd} )")
        else:
            # pre-clone: the base realization IS the session env (content-
            # identical by definition until the first install)
            act, ns = self._base_activation(s, adapter)
            script = f"cd {sdir} && {act} && ( {cmd} )"
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
                pypi: list[str] | None = None, fast: bool = True) -> dict:
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
        if not conda and not pypi:
            raise WeftError("task.invalid", "nothing to install", stage="realize")
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
                            "note": "installed without a solve — the "
                                    "snapshot's full re-solve is the "
                                    "conflict check; pass fast=False to "
                                    "solve at add time"}
                if clone_note:
                    fast_out["materialized_note"] = clone_note
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
            raise WeftError(
                "env.solve_conflict",
                "incremental install failed in session",
                stage="realize",
                hints={"log_tail": (r.err or r.out)[-1500:],
                       "requested": conda + pypi},
            )
        self.store.session_add_deps(session_id, conda, pypi)
        self.store.emit("session.installed", session=session_id,
                        conda=conda, pypi=pypi)
        out = {"installed": {"conda": conda, "pypi": pypi},
               "session_id": session_id}
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
            return {"error": "env.solve_conflict",
                    "detail": (r.err or r.out)[-1500:]}
        return {"method": method}

    def run_installer(self, session_id: str, adapter: SiteAdapter, cmd: str,
                      note: str = "", source: str | None = None) -> dict:
        """The bespoke install that no index can express — an R
        install.packages, a pip install -e, a vendored make install. A
        normal, supported move: it runs in the session AND is captured, so
        `snapshot` can carry it into the spec as a labeled post_install step.

        `source` is a local path (a source tree, a wheel) the command needs:
        weft content-addresses it so the step travels with the env and
        rebuilds ANYWHERE. Without it, a step that reads local paths mints
        an env only this machine can build — the grade will say so."""
        s = self._get(session_id)
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
        if first_clone:
            out["materialized_note"] = (
                "writable prefix cloned on this first installer; running "
                "python kernels see its packages on their next block "
                "(forward hook); R/julia kernels attached before this "
                "need kernel_restart")
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
                or s.get("installers")):
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
    """'numpy >=2' -> 'numpy>=2' (pixi add wants no space)."""
    return dep.replace(" ", "")


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
