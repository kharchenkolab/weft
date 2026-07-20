#!/usr/bin/env python3
"""Weft python kernel driver: a persistent interpreter fed blocks through
files. No sockets — works over any control channel, survives disconnects
(the surrounding job is detached), and leaves a complete transcript.

Protocol (cwd = kernel sandbox):
  blocks/NNNN.code          <- controller writes code
  blocks/NNNN.{out,err}     -> captured streams
  blocks/NNNN.rc            -> exit marker (atomic rename; written last)
  blocks/NNNN.artifacts/    -> $WEFT_BLOCK_DIR for files the block saves
  current_block             -> heartbeat while a block runs
  kernel.stop               <- controller requests shutdown
"""

import contextlib
import io
import os
import signal
import sys
import time
import traceback

# detached launch chains leave SIGINT ignored; restore interruptibility
signal.signal(signal.SIGINT, signal.default_int_handler)

# Lazy-session forward hook: the controller sets WEFT_SESSION_PREFIX when
# this kernel attached BEFORE the session's writable prefix existed. Put
# the future site-packages FIRST (session installs must shadow the base)
# — the dir may not exist yet; invalidate_caches() per block makes its
# later appearance visible to imports, preserving the live-install
# contract with no restart.
_SESSION_PREFIX = os.environ.get("WEFT_SESSION_PREFIX")
if _SESSION_PREFIX:
    sys.path.insert(0, "%s/lib/python%d.%d/site-packages" % (
        _SESSION_PREFIX, sys.version_info[0], sys.version_info[1]))
# pylib variant (cold-base sessions): the layer dir itself IS the site
# dir (pip --target layout) — inserted verbatim
_SESSION_PYLIB = os.environ.get("WEFT_SESSION_PYLIB")
if _SESSION_PYLIB:
    sys.path.insert(0, _SESSION_PYLIB)
    _SESSION_PREFIX = _SESSION_PREFIX or _SESSION_PYLIB  # cache-invalidate flag


class _LiveFile(io.TextIOBase):
    """Write-through capture: the file exists (empty) the moment the
    block starts and GROWS while it runs, so a controller tailing it
    streams output live. Implicit flushes are throttled (~10/s) —
    kernels live on parallel filesystems where a per-print flush storm
    has a real metadata cost; an EXPLICIT flush() (print(flush=True),
    tqdm) is caller intent and always goes through."""

    _THROTTLE_S = 0.1

    def __init__(self, path):
        self._f = open(path, "w")
        self._last = 0.0

    def writable(self):
        return True

    def write(self, s):
        n = self._f.write(s)
        now = time.monotonic()
        if now - self._last >= self._THROTTLE_S:
            self._f.flush()
            self._last = now
        return n

    def flush(self):
        try:
            self._f.flush()
            self._last = time.monotonic()
        except ValueError:
            pass

    def close(self):
        try:
            self._f.flush()
            self._f.close()
        except (ValueError, OSError):
            pass


def main():
    os.makedirs("blocks", exist_ok=True)
    globals_ns = {"__name__": "__main__"}
    n = 0
    while True:
        if os.path.exists("kernel.stop"):
            return 0
        rc_f = f"blocks/{n:04d}.rc"
        code_f = f"blocks/{n:04d}.code"
        if os.path.exists(rc_f):        # done earlier (or driver restarted)
            n += 1
            continue
        if not os.path.exists(code_f):
            time.sleep(0.2)
            continue
        with open("current_block", "w") as f:
            f.write(str(n))
        art = f"blocks/{n:04d}.artifacts"
        os.makedirs(art, exist_ok=True)
        os.environ["WEFT_BLOCK_DIR"] = art
        out = _LiveFile(f"blocks/{n:04d}.out")
        err = _LiveFile(f"blocks/{n:04d}.err")
        rc = 0
        try:
            if _SESSION_PREFIX:
                import importlib
                importlib.invalidate_caches()
            code = open(code_f).read()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                exec(compile(code, f"<block-{n}>", "exec"), globals_ns)
        except SystemExit as e:
            rc = int(e.code or 0)
        except KeyboardInterrupt:
            rc = 130
            err.write("\n[interrupted]\n")
        except BaseException:
            rc = 1
            err.write(traceback.format_exc())
        finally:
            out.close()
            err.close()
        tmp = rc_f + ".tmp"
        open(tmp, "w").write(str(rc))
        os.replace(tmp, rc_f)
        try:
            os.remove("current_block")
        except FileNotFoundError:
            pass
        n += 1


if __name__ == "__main__":
    while True:  # SIGINT between blocks must not kill the kernel
        try:
            sys.exit(main())
        except KeyboardInterrupt:
            continue
