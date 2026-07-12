"""Slurm adapter: the SSH control plane + scheduler verbs (doc 02 §5).

Everything shim-shaped (bootstrap, probe, staging, hashing) is inherited
from SSHAdapter — a cluster login node is just an SSH-reachable POSIX
machine. This class only adds: partition discovery, batch-script
rendering, sbatch/squeue/scontrol/scancel, and the mapping from Slurm job
states to weft's lifecycle vocabulary.
"""

from __future__ import annotations

import re
import shlex

from ..errors import WeftError
from .ssh import SSHAdapter

_SUBMITTED_RE = re.compile(r"Submitted batch job (\d+)")

_QUEUED = {"PENDING", "CONFIGURING", "REQUEUED", "SUSPENDED", "REQUEUE_HOLD"}
_RUNNING = {"RUNNING", "COMPLETING", "STAGE_OUT"}


class SlurmAdapter(SSHAdapter):
    kind = "slurm"

    def __init__(self, *args, account: str | None = None,
                 partition: str | None = None,
                 partitions_allowed: list[str] | None = None,
                 modules_init: str = "", **kw):
        super().__init__(*args, **kw)
        self.account = account
        self.modules_init = modules_init
        from ..policy import allowed_partition
        self.partition = allowed_partition(
            {"partitions_allowed": partitions_allowed} if partitions_allowed else {},
            partition, args[0] if args else "slurm-site",
        )

    # -- probing ---------------------------------------------------------

    def probe(self) -> dict:
        base = super().probe()
        base["scheduler"] = {"type": "slurm", **self._probe_partitions()}
        return base

    def _probe_partitions(self) -> dict:
        r = self.run_cmd(
            "sinfo -h -o '%R|%l|%c|%m|%a' 2>/dev/null | sort -u", timeout=30
        )
        partitions = []
        for line in r.out.splitlines():
            parts = line.strip().split("|")
            if len(parts) != 5:
                continue
            name, timelimit, cpus, mem_mb, avail = parts
            try:
                partitions.append({
                    "name": name,
                    "max_walltime": timelimit,
                    "cpus_per_node": int(cpus),
                    "mem_gb_per_node": max(1, int(mem_mb) // 1024),
                    "available": avail.lower().startswith("up"),
                })
            except ValueError:
                continue
        version = ""
        v = self.run_cmd("sinfo --version 2>/dev/null", timeout=15)
        if v.rc == 0 and v.out.strip():
            version = v.out.strip().split()[-1]
        return {"version": version, "partitions": partitions}

    def module_avail(self, name: str) -> bool:
        """Lazy module-inventory query (doc 02 §3); callers cache."""
        init = (self.modules_init + "; ") if self.modules_init else ""
        r = self.run_cmd(
            init +
            "if ! type module >/dev/null 2>&1; then "
            "[ -f /usr/share/modules/init/sh ] && . /usr/share/modules/init/sh; "
            "[ -f /usr/share/lmod/lmod/init/sh ] && . /usr/share/lmod/lmod/init/sh; fi; "
            f"module avail {shlex.quote(name)} 2>&1 | grep -q {shlex.quote(name)}",
            timeout=30,
        )
        return r.rc == 0

    # -- job control -------------------------------------------------------

    def submit(self, jobdir_rel: str, task: dict) -> str:
        jobdir = self.path(jobdir_rel)
        res = task.get("resources") or {}
        lines = [
            "#!/bin/sh",
            f"#SBATCH --job-name=weft-{jobdir_rel.rsplit('/', 1)[-1]}",
            f"#SBATCH --chdir={jobdir}",
            "#SBATCH --output=slurm-out.log",
            "#SBATCH --error=slurm-out.log",
            f"#SBATCH --cpus-per-task={res.get('cpus', 1)}",
        ]
        if res.get("mem_gb"):
            lines.append(f"#SBATCH --mem={res['mem_gb']}G")
        if res.get("walltime"):
            lines.append(f"#SBATCH --time={res['walltime']}")
        if res.get("gpus"):
            lines.append(f"#SBATCH --gres=gpu:{res['gpus']}")
        if self.partition:
            lines.append(f"#SBATCH --partition={self.partition}")
        if self.account:
            lines.append(f"#SBATCH --account={self.account}")
        # same epilogue contract as the shim's detached runner: files are
        # the source of truth, whatever the scheduler forgets
        lines += [
            "",
            f"cd {shlex.quote(jobdir)}",
            "echo $$ > pid.real",
            "[ -f activate.sh ] && . ./activate.sh",
            'if /usr/bin/time -v true 2>/dev/null; then TIMER="/usr/bin/time -v -o rusage"; else TIMER=""; fi',
            "start=$(date +%s)",
            "$TIMER sh cmd.sh >> log 2>&1",
            "rc=$?",
            "end=$(date +%s)",
            "echo $((end - start)) > wall_s",
            "echo $rc > exit_code.tmp && mv exit_code.tmp exit_code",
        ]
        self.write_file(f"{jobdir_rel}/batch.sh", ("\n".join(lines) + "\n").encode())
        r = self.run_cmd(f"cd {shlex.quote(jobdir)} && sbatch batch.sh", timeout=60)
        m = _SUBMITTED_RE.search(r.out)
        if r.rc != 0 or not m:
            err = (r.err or r.out).strip()
            raise WeftError(
                "sched.rejected",
                f"sbatch refused the submission: {err[-300:]}",
                stage="submit",
                hints={
                    "stderr": err[-800:],
                    "suggestion": "check the resource ask against partition "
                                  "limits (sites.describe shows them)",
                },
            )
        return f"slurm:{m.group(1)}"

    def poll_job(self, handle: str, jobdir_rel: str) -> dict:
        jid = handle.split(":", 1)[1]
        r = self.run_cmd(
            f"squeue -h -j {shlex.quote(jid)} -o %T 2>/dev/null; true",
            timeout=self.poll_timeout,
        )
        state = r.out.strip().split()[0] if r.out.strip() else ""
        if state in _QUEUED:
            return {"state": "queued", "slurm": state}
        if state in _RUNNING:
            return {"state": "running", "slurm": state}

        rc = self.run_cmd(
            f"scontrol show job {shlex.quote(jid)} -o 2>/dev/null; true",
            timeout=self.poll_timeout,
        )
        m = re.search(r"JobState=(\S+)", rc.out)
        slurm_state = m.group(1) if m else ""
        if slurm_state in _QUEUED:
            return {"state": "queued", "slurm": slurm_state}
        if slurm_state in _RUNNING:
            return {"state": "running", "slurm": slurm_state}
        if slurm_state == "TIMEOUT":
            return {**self._file_status(jobdir_rel),
                    "state": "timeout", "slurm": slurm_state}
        if slurm_state == "OUT_OF_MEMORY":
            return {**self._file_status(jobdir_rel),
                    "state": "oom", "slurm": slurm_state}
        if slurm_state == "NODE_FAIL":
            return {"state": "lost", "slurm": slurm_state}
        if slurm_state.startswith("CANCELLED"):
            return {"state": "cancelled", "slurm": slurm_state}

        # COMPLETED / FAILED / job aged out of scontrol: files are the truth
        st = self._file_status(jobdir_rel)
        if st.get("state") == "exited":
            return st
        if slurm_state in ("COMPLETED", "FAILED"):
            m2 = re.search(r"ExitCode=(\d+):", rc.out)
            return {"state": "exited",
                    "exit_code": int(m2.group(1)) if m2 else -1,
                    "wall_s": 0, "note": "exit file missing; used scheduler record"}
        return st

    def _file_status(self, jobdir_rel: str) -> dict:
        try:
            return self.shim(["status", "--dir", self.path(jobdir_rel)],
                             timeout=self.poll_timeout).json()
        except WeftError:
            return {"state": "unknown"}

    def cancel(self, handle: str, jobdir_rel: str) -> None:
        if handle.startswith("slurm:"):
            self.run_cmd(f"scancel {shlex.quote(handle.split(':', 1)[1])}; true",
                         timeout=30)
        else:
            super().cancel(handle, jobdir_rel)
