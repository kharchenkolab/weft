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
        out, err = io.StringIO(), io.StringIO()
        rc = 0
        try:
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
        open(f"blocks/{n:04d}.out", "w").write(out.getvalue())
        open(f"blocks/{n:04d}.err", "w").write(err.getvalue())
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
