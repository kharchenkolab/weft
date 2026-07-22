"""SSH adapter: the shim over the user's own ssh (doc 02 §5).

Weft never stores keys or reimplements transport — it invokes the system
`ssh`, inheriting aliases, ProxyJump, agents, and MFA. A ControlMaster
socket multiplexes every control call over one authenticated connection
(politeness on shared login nodes; MFA answered once per session).

Exit code 255 from ssh is a *transport* failure and maps to
`site.unreachable` (retryable); anything else is the remote command's own
exit status.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from ..errors import WeftError
from .base import ShimResult, SiteAdapter

SHIM_SRC = Path(__file__).resolve().parent.parent / "shim" / "weft-shim"
BOOTSTRAP_VERSION = 5  # v5: shim v7 (pid.epoch same-clock liveness)
                       # v4: shim v6 (list-tree file roots)
                       # v3: CA bundle found on darwin controllers too


import functools


@functools.lru_cache(maxsize=1)
def _controller_ca_bundle() -> Path | None:
    for cand in (
        os.environ.get("SSL_CERT_FILE", ""),
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/cert.pem",
        "/etc/ssl/cert.pem",  # macOS (and BSDs) ship a PEM bundle here
    ):
        if cand and Path(cand).is_file():
            return Path(cand)
    try:
        import certifi
        return Path(certifi.where())
    except ImportError:
        return None


class SSHAdapter(SiteAdapter):
    kind = "ssh"

    def __init__(
        self,
        name: str,
        host: str,
        root: str,
        *,
        user: str | None = None,
        port: int | None = None,
        ssh_opts: list[str] | None = None,
        jump: list[str] | None = None,
        pixi_source: str | None = None,
        pixi_unpack_source: str | None = None,
        connect_timeout: int = 10,
        shared: bool = False,
        pixi_cache: str | None = None,
        transport: str = "ssh",
    ):
        # transport="local": the controller RUNS ON this site (a submit
        # node with the scheduler on PATH) — every command is a direct
        # subprocess, no ssh-to-self (which GSSAPI/2FA-only sites refuse
        # outright). File staging degrades to local-link on the shared
        # (here: same) filesystem. Everything above the primitive
        # surface — scheduler logic, jobdirs, collection, placement —
        # is transport-blind.
        if transport not in ("ssh", "local"):
            raise WeftError("task.invalid",
                            f"unknown transport {transport!r}",
                            stage="infra", hints={"known": ["ssh", "local"]})
        self.transport = transport
        self.shared = shared
        # site-config lever: on netfs-only clusters rattler's cache
        # locking breaks — point the cache at node-local storage. The
        # DEFAULT stays the shared <root>/cache/pixi (cross-build dedupe
        # is what makes rebuild-at-destination cheap).
        self.pixi_cache = pixi_cache
        self.name = name
        self.host = host
        self.user = user
        self.port = port
        self._root = root.rstrip("/")
        self.extra_opts = list(ssh_opts or [])
        # multi-hop: ["user@bastion:port", ...] — rendered as ProxyJump.
        # The target is often only reachable from inside (alien clusters:
        # internet OUT, ssh-only IN); hops are modeled so diagnostics can
        # say WHICH hop died, not just "unreachable".
        self.jump = list(jump or [])
        self.pixi_source = pixi_source
        self.pixi_unpack_source = pixi_unpack_source
        self.connect_timeout = connect_timeout
        # status polls use a tighter timeout so outages surface quickly
        # (a blocked poll is indistinguishable from a slow one until it times out)
        self.poll_timeout = 20.0
        sock_dir = Path(tempfile.gettempdir()) / f"weft-ssh-{os.getuid()}"
        sock_dir.mkdir(mode=0o700, exist_ok=True)
        tag = hashlib.sha256(
            f"{user}@{host}:{port}:{','.join(self.jump)}".encode()
        ).hexdigest()[:16]
        self._control_path = str(sock_dir / tag)

    @property
    def root(self) -> str:
        return self._root

    def destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def _chain_proxy(self, hops: list[str]) -> str | None:
        """Nested ProxyCommand for a hop chain. NOT `-J`: ssh does not pass
        command-line options (keys, host-key policy) down to ProxyJump
        sub-connections, so site-scoped opts would silently not apply at
        the hops — ProxyCommand chains carry them explicitly at every
        level."""
        pc = None
        for hop in hops:
            dest, _, port = hop.partition(":")
            cmd = ["ssh", "-o", "BatchMode=yes",
                   "-o", f"ConnectTimeout={self.connect_timeout}",
                   *self.extra_opts]
            if pc:
                # ssh percent-expands the WHOLE ProxyCommand value at each
                # level — escape the embedded chain so inner %h:%p tokens
                # survive to the level that owns them
                cmd += ["-o", f"ProxyCommand={pc.replace('%', '%%')}"]
            if port:
                cmd += ["-p", port]
            cmd += ["-W", "%h:%p", dest]
            pc = " ".join(shlex.quote(c) for c in cmd)
        return pc

    def _jump_opts(self) -> list[str]:
        pc = self._chain_proxy(self.jump)
        return ["-o", f"ProxyCommand={pc}"] if pc else []

    def _ssh_base(self) -> list[str]:
        opts = [
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={self.connect_timeout}",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self._control_path}",
            "-o", "ControlPersist=120",
            # a master whose LINK died (bastion restart, VPN blip) must
            # exit, or every muxed command fails through it until
            # ControlPersist expires — the classic wedged-mux failure
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2",
        ]
        if self.port:
            opts += ["-p", str(self.port)]
        opts += self._jump_opts() + self.extra_opts
        return ["ssh", *opts, self.destination()]

    def ssh_transport_opts(self) -> list[str]:
        """Options for other ssh-family tools (rsync -e) to share the socket."""
        opts = [
            "-o", "BatchMode=yes",
            "-o", f"ControlPath={self._control_path}",
        ]
        if self.port:
            opts += ["-p", str(self.port)]
        opts += self._jump_opts() + self.extra_opts
        return opts

    def hop_check(self, timeout: int = 6) -> list[dict]:
        """Walk the connection chain hop by hop and report WHERE it dies —
        'bastion up, login refused' is actionable; 'unreachable' is not.
        Each hop is reached through the hops before it, like the real
        connection would."""
        if self.transport == "local":
            return [{"hop": "local", "ok": True,
                     "note": "controller runs on this site; no ssh chain"}]
        chain = [*self.jump, self.destination()
                 + (f":{self.port}" if self.port else "")]
        results = []
        for i, hop in enumerate(chain):
            dest, _, port = hop.partition(":")
            cmd = ["ssh", "-o", "BatchMode=yes",
                   "-o", f"ConnectTimeout={timeout}"]
            pc = self._chain_proxy(chain[:i])
            if pc:
                cmd += ["-o", f"ProxyCommand={pc}"]
            if port:
                cmd += ["-p", port]
            cmd += self.extra_opts + [dest, "echo weft-hop-ok"]
            import subprocess as _sp
            try:
                r = _sp.run(cmd, capture_output=True, text=True,
                            timeout=timeout * (i + 2))
                ok = "weft-hop-ok" in r.stdout
                results.append({
                    "hop": hop, "ok": ok,
                    **({} if ok else {"error": (r.stderr or "")[-200:]})})
            except _sp.TimeoutExpired:
                results.append({"hop": hop, "ok": False, "error": "timeout"})
            if not results[-1]["ok"]:
                for rest in chain[i + 1:]:
                    results.append({"hop": rest, "ok": None,
                                    "note": "not tried (earlier hop failed)"})
                break
        return results

    def _run(
        self, remote_cmd: str, *, input_bytes: bytes | None = None,
        timeout: float = 120.0,
    ) -> ShimResult:
        if self.transport == "local":
            # controller ON the site: same command string, direct shell —
            # what ssh would have handed the remote shell, sh gets here
            try:
                proc = subprocess.run(
                    ["sh", "-c", remote_cmd],
                    input=input_bytes, capture_output=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired as e:
                raise WeftError(
                    "site.unreachable",
                    f"local command on {self.name} timed out after {timeout}s",
                    stage="infra", retryable=True,
                    hints={"command": remote_cmd[:120]},
                ) from e
            return ShimResult(proc.returncode,
                              proc.stdout.decode("utf-8", "replace"),
                              proc.stderr.decode("utf-8", "replace"))
        try:
            proc = subprocess.run(
                [*self._ssh_base(), remote_cmd],
                input=input_bytes, capture_output=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise WeftError(
                "site.unreachable", f"ssh to {self.name} timed out after {timeout}s",
                stage="infra", retryable=True,
                hints={"host": self.host, "command": remote_cmd[:120]},
            ) from e
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        if proc.returncode == 255:
            # a stale mux master (link died under it) poisons every retry
            # until ControlPersist expires — evict it so the NEXT attempt
            # builds a fresh connection instead of failing through the corpse
            subprocess.run(
                ["ssh", "-o", f"ControlPath={self._control_path}",
                 "-O", "exit", self.destination()],
                capture_output=True, timeout=10)
            raise WeftError(
                "site.unreachable", f"ssh transport to {self.name} failed",
                stage="infra", retryable=True,
                hints={"host": self.host, "stderr": err[-500:],
                       "note": "connection multiplexer reset; a retry "
                               "builds a fresh connection"},
            )
        return ShimResult(proc.returncode, out, err)

    def _env_prefix(self) -> str:
        # shared roots: everything created must be group-usable
        # sites without a system CA store (minimal images) break every
        # TLS-using tool; the bootstrap pushes the controller's bundle.
        # Only point at it when the controller actually HAD one to push —
        # an SSL_CERT_FILE aimed at a never-pushed path breaks TLS harder
        # than no bundle at all (found via a darwin controller: no PEM,
        # nothing pushed, every remote https "failed" instantly).
        ca = (f"SSL_CERT_FILE={shlex.quote(self.path('etc/cacert.pem'))} "
              if _controller_ca_bundle() is not None else "")
        return (("umask 002; " if self.shared else "") +
            f"WEFT_ROOT={shlex.quote(self.root)} "
            f"PIXI_CACHE_DIR="
            f"{shlex.quote(self.pixi_cache or self.path('cache/pixi'))} "
            f"PIXI_HOME={shlex.quote(self.path('pixi-home'))} "
            # pip/uv HTTP+wheel caches: a property of the SITE ROOT, not
            # of whatever HOME the deployment has — warm once, warm for
            # every session (and every user, on shared roots). Safe to
            # rm -rf anytime; the first cold add repays it.
            f"PIP_CACHE_DIR={shlex.quote(self.path('cache/pip'))} "
            f"UV_CACHE_DIR={shlex.quote(self.path('cache/uv'))} "
            + ca +
            f"PATH={shlex.quote(self.path('bin'))}:$PATH "
        )

    # -- SiteAdapter interface ------------------------------------------------

    def ensure_bootstrap(self) -> None:
        marker = self.run_cmd(f"cat {shlex.quote(self.path('.weft-site'))} 2>/dev/null")
        if marker.rc == 0 and f'"bootstrap_version": {BOOTSTRAP_VERSION}' in marker.out:
            shim_ok = self.run_cmd(
                f"sha256sum {shlex.quote(self.path('bin/weft-shim'))} 2>/dev/null"
            )
            local_hash = hashlib.sha256(SHIM_SRC.read_bytes()).hexdigest()
            if shim_ok.rc == 0 and shim_ok.out.split()[0] == local_hash:
                return
        r = self.run_cmd(
            f"mkdir -p {shlex.quote(self.root)}/bin "
            f"{shlex.quote(self.root)}/envs {shlex.quote(self.root)}/cas "
            f"{shlex.quote(self.root)}/jobs {shlex.quote(self.root)}/tmp "
            f"{shlex.quote(self.root)}/cache"
        )
        if r.rc != 0:
            raise WeftError(
                "site.bootstrap_failed",
                f"cannot create weft root on {self.name}: {r.err[:300]}",
                stage="infra", hints={"root": self.root},
            )
        self.write_file("bin/weft-shim", SHIM_SRC.read_bytes(), mode=0o755)
        ca = _controller_ca_bundle()
        if ca is not None:
            self.write_file("etc/cacert.pem", ca.read_bytes())
        if self.pixi_source and not self.file_exists("bin/pixi"):
            self._push_binary(Path(self.pixi_source), "bin/pixi")
        if self.pixi_unpack_source and not self.file_exists("bin/pixi-unpack"):
            self._push_binary(Path(self.pixi_unpack_source), "bin/pixi-unpack")
        smoke = self.shim(["version"])
        if smoke.rc != 0:
            raise WeftError(
                "site.bootstrap_failed", f"shim smoke test failed: {smoke.err[:300]}",
                stage="infra",
            )
        self.write_file(
            ".weft-site", f'{{"bootstrap_version": {BOOTSTRAP_VERSION}}}\n'.encode()
        )

    def _push_binary(self, local: Path, rel: str) -> None:
        digest = hashlib.sha256(local.read_bytes()).hexdigest()
        dest = self.path(rel)
        r = self._run(
            f"cat > {shlex.quote(dest)}.tmp && "
            # busybox sha256sum has no --quiet; discard the OK lines instead
            f"echo {digest}  {shlex.quote(dest)}.tmp | sha256sum -c - >/dev/null && "
            f"chmod 755 {shlex.quote(dest)}.tmp && mv {shlex.quote(dest)}.tmp {shlex.quote(dest)}",
            input_bytes=local.read_bytes(), timeout=600,
        )
        if r.rc != 0:
            raise WeftError(
                "site.bootstrap_failed", f"failed to push {rel}: {r.err[:300]}",
                stage="infra", retryable=True,
            )

    def shim(self, argv: list[str], *, timeout: float = 60.0) -> ShimResult:
        cmd = self._env_prefix() + shlex.join(
            [self.path("bin/weft-shim"), *argv]
        )
        return self._run(cmd, timeout=timeout)

    def run_cmd(self, script: str, *, timeout: float = 120.0) -> ShimResult:
        return self._run(self._env_prefix() + "sh -c " + shlex.quote(script),
                         timeout=timeout)

    def write_file(self, rel: str, data: bytes, mode: int = 0o644) -> None:
        if self.shared:
            mode |= 0o020  # group-writable on shared roots
        dest = self.path(rel)
        # Atomic publish: stage to a tmp sibling, verify the bytes survived
        # the pipe, then rename into place. `cat > dest` truncates the file at
        # redirect-open — long before the payload arrives over ssh — and the
        # kernel driver's exists→read loop can observe that window, exec an
        # empty/partial block, and report rc=0 (bug2). rename(2) within one
        # dir is the same contract the driver already keeps for its .rc.
        tmp = f"{dest}.wtmp.{os.urandom(4).hex()}"
        digest = hashlib.sha256(data).hexdigest()
        qt, qd = shlex.quote(tmp), shlex.quote(dest)
        r = self._run(
            f"mkdir -p {shlex.quote(os.path.dirname(dest))} && "
            f"cat > {qt} && "
            # busybox sha256sum has no --quiet; discard the OK lines instead
            f"echo {digest}  {qt} | sha256sum -c - >/dev/null && "
            f"chmod {mode:o} {qt} && mv -f {qt} {qd} || {{ rm -f {qt}; exit 1; }}",
            input_bytes=data, timeout=120,
        )
        if r.rc != 0:
            raise WeftError(
                "site.unreachable", f"write_file {rel} failed: {r.err[:300]}",
                stage="infra", retryable=True,
            )

    def read_file(self, rel: str, max_bytes: int | None = None) -> bytes:
        capped = f"head -c {max_bytes} " if max_bytes else "cat "
        r = self._run(capped + shlex.quote(self.path(rel)), timeout=120)
        if r.rc != 0:
            raise WeftError("data.missing", f"no such file on site: {rel}", stage="infra")
        return r.out.encode("utf-8", "surrogateescape")

    def transfer_endpoint(self) -> dict:
        if self.transport == "local":
            # same machine: staging is hardlink/copy, never a wire
            return {"method": "local-link", "cas_root": self.path("cas")}
        if not hasattr(self, "_remote_rsync"):
            local = subprocess.run(["sh", "-c", "command -v rsync"],
                                   capture_output=True).returncode == 0
            remote = self.run_cmd("command -v rsync >/dev/null 2>&1 && echo yes"
                                  ).out.strip() == "yes"
            self._remote_rsync = local and remote
        return {
            # tar-over-ssh fallback keeps bare sites usable (no rsync there)
            "method": "rsync-ssh" if self._remote_rsync else "ssh-pipe",
            "destination": self.destination(),
            "cas_root": self.path("cas"),
            "ssh_opts": self.ssh_transport_opts(),
        }

    def submit(self, jobdir_rel: str, task: dict) -> str:
        r = self.shim(["run", "--dir", self.path(jobdir_rel)])
        if r.rc != 0:
            raise WeftError(
                "job.nonzero_exit", f"shim run failed: {(r.err or r.out)[:300]}",
                stage="submit",
            )
        return f"pid:{r.json().get('pid', 0)}"

    def poll_job(self, handle: str, jobdir_rel: str) -> dict:
        return self.shim(["status", "--dir", self.path(jobdir_rel)],
                         timeout=self.poll_timeout).json()

    def poll_jobs(self, items: list[tuple[str, str]]) -> dict[str, dict]:
        if not items:
            return {}
        by_dir = {self.path(rel): h for h, rel in items}
        stdin = ("\n".join(by_dir) + "\n").encode()
        r = self._run(
            self._env_prefix() + shlex.join(
                [self.path("bin/weft-shim"), "status-batch"]),
            input_bytes=stdin,
            timeout=max(self.poll_timeout, 5 + 0.05 * len(items)),
        )
        if r.rc != 0:
            raise WeftError(
                "site.unreachable", f"status-batch failed: {r.err[-300:]}",
                stage="infra", retryable=True,
            )
        out: dict[str, dict] = {}
        for line in r.out.splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            handle = by_dir.get(rec.pop("dir", ""))
            if handle is not None:
                out[handle] = rec
        # anything the shim didn't answer for counts as unknown, not missing
        for h, _ in items:
            out.setdefault(h, {"state": "unknown"})
        return out

    def cancel(self, handle: str, jobdir_rel: str) -> None:
        if handle.startswith("pid:"):
            pid = handle[4:]
            self.run_cmd(f"kill -s TERM -- -{shlex.quote(pid)} 2>/dev/null; true")

    def close_control(self) -> None:
        """Drop the multiplexed connection (used by chaos tests)."""
        subprocess.run(
            ["ssh", "-o", f"ControlPath={self._control_path}",
             "-O", "exit", self.destination()],
            capture_output=True,
        )
