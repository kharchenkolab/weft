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


def parse_gres(s: str) -> list[dict]:
    """'gpu:fake:2(S:0-1),shard:4' → [{type: gpu, model: fake, count: 2},
    {type: shard, model: None, count: 4}]; '(null)'/'' → []."""
    out = []
    for tok in (s or "").split(","):
        tok = tok.strip()
        if not tok or tok.startswith("(null"):
            continue
        tok = re.sub(r"\(.*?\)$", "", tok)     # strip socket/index suffix
        parts = tok.split(":")
        try:
            count = int(parts[-1])
        except ValueError:
            continue
        out.append({"type": parts[0],
                    "model": parts[1] if len(parts) > 2 else None,
                    "count": count})
    return out


def parse_tres(s: str) -> dict:
    """'cpu=32,gres/gpu=2,mem=64G' → {cpu: 32, gpu: 2, mem: '64G'}."""
    out: dict = {}
    for tok in (s or "").split(","):
        if "=" not in tok:
            continue
        k, v = tok.split("=", 1)
        k = k.strip().replace("gres/", "")
        try:
            out[k] = int(v)
        except ValueError:
            out[k] = v.strip()
    return out


def _expand_array_ids(token: str) -> list[str]:
    """'123_[0-2,5%4]' -> ['123_0','123_1','123_2','123_5']; plain ids pass
    through. Pending array elements only exist in this compact form."""
    m = re.fullmatch(r"(\d+)_\[([^\]]+)\]", token)
    if not m:
        return [token]
    jid, spec = m.group(1), m.group(2).split("%")[0]
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(f"{jid}_{i}" for i in range(int(a), int(b) + 1))
        elif part:
            out.append(f"{jid}_{part}")
    return out


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
            "sinfo -h -o '%R|%l|%c|%m|%a|%G|%f' 2>/dev/null | sort -u",
            timeout=30)
        partitions = []
        for line in r.out.splitlines():
            parts = line.strip().split("|")
            if len(parts) != 7:
                continue
            name, timelimit, cpus, mem_mb, avail, gres, feats = parts
            try:
                partitions.append({
                    "name": name,
                    "max_walltime": timelimit,
                    "cpus_per_node": int(cpus),
                    "mem_gb_per_node": max(1, int(mem_mb) // 1024),
                    "available": avail.lower().startswith("up"),
                    "gres": parse_gres(gres),
                    "features": [] if feats in ("", "(null)")
                    else sorted(set(feats.split(","))),
                })
            except ValueError:
                continue
        # scontrol has the partition facts sinfo cannot show: who may use
        # which QOS, defaults, priority — the "am I allowed" half
        detail = self._partition_detail()
        for p in partitions:
            p.update(detail.get(p["name"], {}))
        version = ""
        v = self.run_cmd("sinfo --version 2>/dev/null", timeout=15)
        if v.rc == 0 and v.out.strip():
            version = v.out.strip().split()[-1]
        return {"version": version, "partitions": partitions}

    def _partition_detail(self) -> dict[str, dict]:
        r = self.run_cmd("scontrol show partition -o 2>/dev/null", timeout=30)
        out: dict[str, dict] = {}
        for line in r.out.splitlines():
            kv = dict(t.split("=", 1) for t in line.split() if "=" in t)
            name = kv.get("PartitionName")
            if not name:
                continue
            entry = {}
            if kv.get("AllowQos") not in (None, "ALL"):
                entry["allow_qos"] = kv["AllowQos"].split(",")
            if kv.get("QoS") not in (None, "N/A"):
                entry["partition_qos"] = kv["QoS"]
            if kv.get("DefaultTime") not in (None, "NONE"):
                entry["default_walltime"] = kv["DefaultTime"]
            if kv.get("MaxNodes") not in (None, "UNLIMITED"):
                try:
                    entry["max_nodes"] = int(kv["MaxNodes"])
                except ValueError:
                    pass
            if kv.get("PriorityTier"):
                try:
                    entry["priority_tier"] = int(kv["PriorityTier"])
                except ValueError:
                    pass
            if kv.get("OverSubscribe"):
                entry["oversubscribe"] = kv["OverSubscribe"]
            out[name] = entry
        return out

    def associations(self) -> dict:
        """What am *I* allowed to ask for, where — accounts, QOS (with
        structured ceilings), fairshare. Every field is None when the
        cluster has no accounting DB: 'unknown' is not 'unlimited'."""
        assoc = self.run_cmd(
            "sacctmgr -nP show assoc where user=\"$USER\" "
            "format=account,user,partition,qos,defaultqos 2>/dev/null; true",
            timeout=30)
        associations = []
        for ln in assoc.out.splitlines():
            f = ln.split("|")
            if len(f) < 5 or not f[0]:
                continue
            associations.append({
                "account": f[0],
                "partition": f[2] or None,     # None = any partition
                "allowed_qos": [q for q in f[3].split(",") if q],
                "default_qos": f[4] or None,
            })
        qos_r = self.run_cmd(
            "sacctmgr -nP show qos "
            "format=name,maxwall,maxtresperuser,priority,maxjobspu "
            "2>/dev/null; true", timeout=30)
        qos = []
        for ln in qos_r.out.splitlines():
            f = ln.split("|")
            if len(f) < 3 or not f[0]:
                continue
            entry = {"name": f[0], "max_wall": f[1] or None,
                     "limits_per_user": parse_tres(f[2])}
            if len(f) > 3 and f[3]:
                try:
                    entry["priority"] = int(f[3])
                except ValueError:
                    pass
            if len(f) > 4 and f[4]:
                try:
                    entry["max_jobs_per_user"] = int(f[4])
                except ValueError:
                    pass
            qos.append(entry)
        share = self.run_cmd(
            "sshare -nP -U format=account,user,rawshares,fairshare "
            "2>/dev/null; true", timeout=30)
        fairshare = None
        for ln in share.out.splitlines():
            f = ln.split("|")
            if len(f) >= 4 and f[0]:
                try:
                    fairshare = {"account": f[0], "raw_shares": int(f[2]),
                                 "factor": float(f[3])}
                except ValueError:
                    continue
                break
        return {
            "associations": associations or None,
            "qos": qos or None,
            "fairshare": fairshare,
            "note": None if associations else
            "no accounting DB visible: limits are UNKNOWN, not unlimited — "
            "submit small and observe, or ask the site owner",
        }

    def load(self) -> dict:
        """Login-node load + the scheduler's live picture: idle vs allocated
        CPUs per partition, queue backlog, QOS (when accounting exists),
        and the caller's own footprint. This — not the capability record —
        is what placement and wait estimation should reason over."""
        base = super().load()
        base["login_note"] = "load figures describe the login node only"
        parts: dict[str, dict] = {}
        r = self.run_cmd("sinfo -h -o '%R|%C' 2>/dev/null | sort -u", timeout=30)
        for line in r.out.splitlines():
            try:
                name, cpus = line.strip().split("|")
                alloc, idle, other, total = (int(x) for x in cpus.split("/"))
                parts[name] = {"cpus_idle": idle, "cpus_allocated": alloc,
                               "cpus_down": other, "cpus_total": total,
                               "pending_jobs": 0, "running_jobs": 0}
            except ValueError:
                continue
        q = self.run_cmd("squeue -h -o '%P %T' 2>/dev/null", timeout=30)
        for line in q.out.splitlines():
            fields = line.split()
            if len(fields) != 2:
                continue
            p = parts.setdefault(fields[0], {"pending_jobs": 0, "running_jobs": 0})
            if fields[1] == "PENDING":
                p["pending_jobs"] = p.get("pending_jobs", 0) + 1
            elif fields[1] == "RUNNING":
                p["running_jobs"] = p.get("running_jobs", 0) + 1
        # GPUs are usually the limiting factor: configured vs in-use GRES
        # per partition (nodewise; a node in several partitions counts in
        # each — same convention sinfo itself uses)
        g = self.run_cmd(
            "sinfo -h -N -O 'PartitionName:30,Gres:40,GresUsed:40' "
            "2>/dev/null", timeout=30)
        for line in g.out.splitlines():
            f = line.split()
            if len(f) < 2:
                continue
            pname = f[0]
            conf = parse_gres(f[1]) if len(f) > 1 else []
            used = parse_gres(f[2]) if len(f) > 2 else []
            total = sum(x["count"] for x in conf if x["type"] == "gpu")
            busy = sum(x["count"] for x in used if x["type"] == "gpu")
            if total and pname in parts:
                p = parts[pname]
                p["gpus_total"] = p.get("gpus_total", 0) + total
                p["gpus_allocated"] = p.get("gpus_allocated", 0) + busy
                p["gpus_idle"] = p["gpus_total"] - p["gpus_allocated"]
        base["partitions"] = parts
        mine = self.run_cmd("squeue -h -u \"$USER\" -o '%T' 2>/dev/null", timeout=30)
        my = mine.out.split()
        base["my_jobs"] = {"pending": my.count("PENDING"),
                           "running": my.count("RUNNING")}
        acct = self.associations()
        base["qos"] = acct["qos"]      # None = no accounting DB; not "no limits"
        base["my_associations"] = acct["associations"]
        base["fairshare"] = acct["fairshare"]
        return base

    def estimate_start(self, resources: dict,
                       partition: str | None = None) -> dict:
        """Scheduler-computed start ETA under current load and priorities,
        via `sbatch --test-only` — nothing is submitted."""
        directives = [f"--cpus-per-task={resources.get('cpus', 1)}"]
        if resources.get("mem_gb"):
            directives.append(f"--mem={resources['mem_gb']}G")
        if resources.get("walltime"):
            directives.append(f"--time={resources['walltime']}")
        if resources.get("gpus"):
            directives.append(f"--gres=gpu:{resources['gpus']}")
        if partition or self.partition:
            directives.append(f"--partition={partition or self.partition}")
        r = self.run_cmd(
            "printf '#!/bin/sh\\ntrue\\n' | sbatch --test-only "
            + " ".join(directives) + " 2>&1; true", timeout=30,
        )
        m = re.search(r"to start at (\S+)", r.out)
        if m:
            return {"estimated_start": m.group(1), "raw": r.out.strip()[:200]}
        return {"estimated_start": None,
                "note": "scheduler gave no estimate (rejected ask or busy)",
                "raw": r.out.strip()[:300]}

    def module_system_status(self) -> str:
        """'ok' | 'not_initialized' — distinguishes "module missing" from
        "module command itself unavailable in non-interactive shells"."""
        init = (self.modules_init + "; ") if self.modules_init else ""
        r = self.run_cmd(
            init +
            "if ! type module >/dev/null 2>&1; then "
            "[ -f /usr/share/modules/init/sh ] && . /usr/share/modules/init/sh; "
            "[ -f /usr/share/lmod/lmod/init/sh ] && . /usr/share/lmod/lmod/init/sh; fi; "
            "type module >/dev/null 2>&1 && echo ok || echo missing",
            timeout=30,
        )
        return "ok" if r.out.strip().endswith("ok") else "not_initialized"

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

    def module_inventory(self) -> list[str]:
        """The whole module list — DISCOVERY, not verification: on a new
        cluster the agent needs to find what CUDA/MPI/compilers the site
        offers, not guess names to check. `-t` gives one name per line;
        `module` chats on stderr by convention."""
        init = (self.modules_init + "; ") if self.modules_init else ""
        r = self.run_cmd(
            init +
            "if ! type module >/dev/null 2>&1; then "
            "[ -f /usr/share/modules/init/sh ] && . /usr/share/modules/init/sh; "
            "[ -f /usr/share/lmod/lmod/init/sh ] && . /usr/share/lmod/lmod/init/sh; fi; "
            "module -t avail 2>&1",
            timeout=60,
        )
        names = []
        for ln in r.out.splitlines():
            ln = ln.strip()
            if not ln or ln.endswith(":") or ln.startswith("-"):
                continue      # section headers ("/opt/site-modules:")
            names.append(ln.rstrip("(default)").strip())
        return sorted(set(names))

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
            # conda activation hooks may contain bashisms
            'if [ -z "$WEFT_BASH" ] && command -v bash >/dev/null 2>&1; then',
            '    WEFT_BASH=1 exec bash "$0" "$@"; fi',
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

    supports_native_array = True

    def submit_array(self, group_rel: str, task: dict, n: int) -> str:
        """ONE submission for the whole scan: `--array=0..n-1`; each element
        materializes its own sandbox node-side from the shared plan, so
        submission cost is O(1) in n (login-node politeness, doc 02 §5)."""
        gdir = self.path(group_rel)
        res = task.get("resources") or {}
        lines = [
            "#!/bin/sh",
            f"#SBATCH --job-name=weft-{group_rel.rsplit('/', 1)[-1]}",
            f"#SBATCH --array=0-{n - 1}",
            f"#SBATCH --chdir={gdir}",
            f"#SBATCH --output={gdir}/slurm-%a.log",
            f"#SBATCH --error={gdir}/slurm-%a.log",
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
        lines += [
            "",
            'if [ -z "$WEFT_BASH" ] && command -v bash >/dev/null 2>&1; then',
            '    WEFT_BASH=1 exec bash "$0" "$@"; fi',
            'E="el$SLURM_ARRAY_TASK_ID"',
            f'mkdir -p "$E" && cd "$E"',
            "echo $$ > pid.real",
            f"[ -f {gdir}/inputs.tsv ] && "
            f"{shlex.quote(self.path('bin/weft-shim'))} materialize "
            f"--cas {shlex.quote(self.transfer_endpoint()['cas_root'])} "
            f"--dir . --plan {gdir}/inputs.tsv >> log 2>&1",
            f"[ -f {gdir}/activate.sh ] && . {gdir}/activate.sh",
            "export WEFT_ARRAY_INDEX=$SLURM_ARRAY_TASK_ID",
            'if /usr/bin/time -v true 2>/dev/null; then TIMER="/usr/bin/time -v -o rusage"; else TIMER=""; fi',
            "start=$(date +%s)",
            f"$TIMER sh {gdir}/cmd.sh >> log 2>&1",
            "rc=$?",
            "end=$(date +%s)",
            "echo $((end - start)) > wall_s",
            "echo $rc > exit_code.tmp && mv exit_code.tmp exit_code",
        ]
        self.write_file(f"{group_rel}/batch.sh", ("\n".join(lines) + "\n").encode())
        r = self.run_cmd(f"cd {shlex.quote(gdir)} && sbatch batch.sh", timeout=60)
        m = _SUBMITTED_RE.search(r.out)
        if r.rc != 0 or not m:
            raise WeftError(
                "sched.rejected",
                f"sbatch refused the array: {(r.err or r.out)[-300:]}",
                stage="submit",
                hints={"suggestion": "check the ask against partition limits"},
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

    _CHUNK = 400  # scheduler ids per command line

    def poll_jobs(self, items: list[tuple[str, str]]) -> dict[str, dict]:
        """One squeue (+ one scontrol for departed ids) per tick for ALL
        outstanding jobs — the login-node politeness contract. Exit files
        are consulted only for jobs at a terminal transition."""
        slurm_items = [(h, rel) for h, rel in items if h.startswith("slurm:")]
        other = [(h, rel) for h, rel in items if not h.startswith("slurm:")]
        out: dict[str, dict] = {}
        if other:
            out.update(super().poll_jobs(other))
        if not slurm_items:
            return out

        by_jid = {h.split(":", 1)[1]: (h, rel) for h, rel in slurm_items}
        queue_state: dict[str, str] = {}
        queue_reason: dict[str, str] = {}
        jids = list(by_jid)
        for i in range(0, len(jids), self._CHUNK):
            chunk = ",".join(jids[i : i + self._CHUNK])
            r = self.run_cmd(
                f"squeue -h -j {shlex.quote(chunk)} -o '%i|%T|%r' 2>/dev/null; true",
                timeout=self.poll_timeout,
            )
            for line in r.out.splitlines():
                parts = line.strip().split("|")
                if len(parts) >= 2:
                    for eid in _expand_array_ids(parts[0]):
                        queue_state[eid] = parts[1]
                        if len(parts) >= 3 and parts[2] not in ("None", ""):
                            # PENDING reason names the workaround: Priority,
                            # Resources, QOSMax*, PartitionTimeLimit, ...
                            queue_reason[eid] = parts[2]

        departed = [j for j in jids if j not in queue_state]
        ctl_state: dict[str, str] = {}
        for i in range(0, len(departed), self._CHUNK):
            chunk = ",".join(departed[i : i + self._CHUNK])
            r = self.run_cmd(
                f"scontrol show job {shlex.quote(chunk)} -o 2>/dev/null; true",
                timeout=self.poll_timeout,
            )
            for block in r.out.splitlines():
                mst = re.search(r"JobState=(\S+)", block)
                if not mst:
                    continue
                # array elements: the real JobId differs — key by
                # ArrayJobId_ArrayTaskId, which is what we track
                maj = re.search(r"ArrayJobId=(\d+)", block)
                mat = re.search(r"ArrayTaskId=([\d,\-%]+)", block)
                if maj and mat:
                    for eid in _expand_array_ids(
                            f"{maj.group(1)}_[{mat.group(1)}]"):
                        ctl_state[eid] = mst.group(1)
                    continue
                mid = re.search(r"JobId=(\d+)", block)
                if mid:
                    ctl_state[mid.group(1)] = mst.group(1)

        # exit files, fetched in one shim call, for jobs the scheduler says
        # are finished (or has forgotten entirely)
        need_files = [
            j for j in departed
            if ctl_state.get(j, "") not in _QUEUED | _RUNNING
        ]
        file_status: dict[str, dict] = {}
        if need_files:
            batch = super().poll_jobs(
                [(by_jid[j][0], by_jid[j][1]) for j in need_files]
            )
            file_status = {j: batch[by_jid[j][0]] for j in need_files}

        for jid, (handle, rel) in by_jid.items():
            st = queue_state.get(jid) or ctl_state.get(jid, "")
            if st in _QUEUED:
                out[handle] = {"state": "queued", "slurm": st,
                               "reason": queue_reason.get(jid, "")}
            elif st in _RUNNING:
                out[handle] = {"state": "running", "slurm": st}
            elif st == "TIMEOUT":
                out[handle] = {**file_status.get(jid, {}),
                               "state": "timeout", "slurm": st}
            elif st == "OUT_OF_MEMORY":
                out[handle] = {**file_status.get(jid, {}),
                               "state": "oom", "slurm": st}
            elif st == "NODE_FAIL":
                out[handle] = {"state": "lost", "slurm": st}
            elif st.startswith("CANCELLED"):
                out[handle] = {"state": "cancelled", "slurm": st}
            else:
                fs = file_status.get(jid, {"state": "unknown"})
                if fs.get("state") != "exited" and st in ("COMPLETED", "FAILED"):
                    out[handle] = {"state": "exited", "exit_code": -1,
                                   "wall_s": 0,
                                   "note": "exit file missing; scheduler record"}
                else:
                    out[handle] = fs
        return out

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
