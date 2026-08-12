"""Task model: the pure execution request, content-hashable for memoization.

TaskID covers what determines the *result*: environment identity, input
refs and their mount points, code, command, declared outputs, array shape,
env vars. Site and resources are deliberately excluded — the same task on
a different site or with a bigger memory ask is the same computation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import WeftError
from .ids import task_id

_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class TaskInput:
    ref: str
    mount_as: str


@dataclass
class Resources:
    cpus: int = 1
    mem_gb: int = 0          # 0 = unspecified
    gpus: int = 0
    walltime: str = ""       # "HH:MM:SS", empty = unspecified
    partition: str = ""      # scheduler partition; empty = site default
    # raw scheduler directives for THIS task (e.g. "--constraint=ib") —
    # the per-task escape hatch for site quirks weft cannot know about.
    # Validated at submit (weft-managed and dangerous flags refused);
    # hash-neutral like all resources: they say WHERE/HOW, not WHAT.
    scheduler_directives: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {"cpus": self.cpus, "mem_gb": self.mem_gb, "gpus": self.gpus,
               "walltime": self.walltime}
        if self.partition:
            out["partition"] = self.partition
        if self.scheduler_directives:
            out["scheduler_directives"] = self.scheduler_directives
        return out


@dataclass
class Task:
    command: str
    env: str | None = None            # EnvID; None = bare site environment
    inputs: list[TaskInput] = field(default_factory=list)
    code: TaskInput | None = None     # code is just data (doc 01 §2)
    outputs: list[str] = field(default_factory=list)
    resources: Resources = field(default_factory=Resources)
    site: str = "auto"
    array: int | None = None          # N elements; WEFT_ARRAY_INDEX in [0, N)
    env_vars: dict[str, str] = field(default_factory=dict)
    # control-flow chaining: job_ids that must finish (DONE) first —
    # held CONTROLLER-side on every site kind (no sbatch --dependency
    # translation exists; one holder, one grammar). NOT part of
    # task_hash: dependencies say WHEN a task may run, not WHAT it
    # computes.
    after: list[str] = field(default_factory=list)
    # human handle for lists/events ("calibrate run 3"), ≤200 chars. NOT
    # part of task_hash: a label says what to CALL a task, not what it
    # computes — relabeling never forks memoization, and two identically
    # labeled tasks still memoize against each other.
    label: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        d = dict(d.get("task", d))
        unknown = set(d) - {
            "command", "env", "inputs", "code", "outputs", "resources",
            "site", "array", "env_vars", "after", "label",
        }
        if unknown:
            raise WeftError(
                "task.invalid", f"unknown task fields: {sorted(unknown)}",
                stage="submit",
                hints={"known_fields": [
                    "command", "env", "inputs", "code", "outputs",
                    "resources", "site", "array", "env_vars", "after",
                    "label"]},
            )
        if not d.get("command"):
            raise WeftError("task.invalid", "task.command is required", stage="submit")

        def _input(x) -> TaskInput:
            return TaskInput(ref=x["ref"], mount_as=x["mount_as"])

        res = d.get("resources") or {}
        t = cls(
            command=str(d["command"]),
            env=d.get("env"),
            inputs=[_input(x) for x in d.get("inputs", [])],
            code=_input(d["code"]) if d.get("code") else None,
            outputs=[str(o) for o in d.get("outputs", [])],
            resources=Resources(
                cpus=int(res.get("cpus", 1)),
                mem_gb=int(res.get("mem_gb", 0)),
                gpus=int(res.get("gpus", 0)),
                walltime=str(res.get("walltime", "")),
                partition=str(res.get("partition", "")),
                scheduler_directives=[str(x) for x in
                                      res.get("scheduler_directives", [])],
            ),
            site=d.get("site", "auto"),
            array=int(d["array"]) if d.get("array") else None,
            env_vars={k: str(v) for k, v in (d.get("env_vars") or {}).items()},
            after=[str(x) for x in d.get("after", [])],
            label=str(d.get("label", "")),
        )
        t.validate()
        return t

    def validate(self) -> None:
        if self.resources.scheduler_directives:
            # fail FAST at submit, not in the drive thread. Slurm is the
            # only scheduler today; dispatch per-adapter when that changes.
            from .adapters.slurm import validate_directives
            validate_directives(self.resources.scheduler_directives,
                                "resources.scheduler_directives")
        for k in self.env_vars:
            # KEYS are spliced into cmd.sh as `export {k}=...` — the value
            # is shlex-quoted but a key can only be quoted by refusing
            # anything that is not a NAME (a newline in a key would run
            # as its own shell line — 2026-07 injection sweep, silent)
            if not _ENV_KEY_RE.fullmatch(k):
                raise WeftError(
                    "task.invalid",
                    f"env_vars key {k!r} is not a valid shell identifier",
                    stage="submit",
                    hints={"rule": "[A-Za-z_][A-Za-z0-9_]*"})
            if k.startswith("WEFT_") and k != "WEFT_ARRAY_INDEX":
                # reserved facts namespace: these export AFTER the
                # WEFT_* lines in cmd.sh, so a user key here silently
                # clobbers weft's guaranteed variables (WEFT_CPUS,
                # WEFT_JOB_ID, WEFT_STORAGE_*) for the job's own tools.
                # The documented overrides (LC_*, PYTHONNOUSERSITE) are
                # deliberate levers; WEFT_ is not. WEFT_ARRAY_INDEX is
                # exempt: weft's own array merge stamps it into element
                # env_vars, and the job row round-trips through
                # from_dict at drive time — the gate must not refuse
                # weft's own stamp (a user-set value is overwritten by
                # that same merge anyway).
                raise WeftError(
                    "task.invalid",
                    f"env_vars key {k!r} is in the reserved WEFT_ "
                    "namespace",
                    stage="submit",
                    hints={"reserved": "WEFT_*",
                           "suggestion": "resources/policy set the real "
                                         "facts; pick a different name "
                                         "for your own variable"})
        if len(self.label) > 200:
            raise WeftError(
                "task.invalid",
                f"label is {len(self.label)} chars (max 200) — labels are "
                "display handles, not documents", stage="submit")
        for mount in [i.mount_as for i in self.inputs] + (
            [self.code.mount_as] if self.code else []
        ) + list(self.outputs):
            if mount.startswith("/") or ".." in mount.split("/"):
                raise WeftError(
                    "task.invalid",
                    f"paths must be sandbox-relative without '..': {mount!r}",
                    stage="submit",
                )
            if "\t" in mount or "\n" in mount:
                # these paths travel in TSV plans (inputs.tsv) — a tab or
                # newline corrupts the row, and the failure would come
                # back wearing a transfer/verify code
                raise WeftError(
                    "task.invalid",
                    f"paths must not contain tab/newline: {mount!r}",
                    stage="submit",
                )
        # ONE in-job path, one writer. Two inputs landing on the same
        # path silently last-writer-win in the job dir (live campaign:
        # two run+rel inputs both defaulted to 'status.json'; the model
        # discovered the clobber only by submitting a probe job). And an
        # OUTPUT at an input's mount path is worse than a collision:
        # inputs are hardlink-shared and read-only by contract — writing
        # there can falsify the content-addressed record.
        mounts: dict[str, str] = {}
        for label, m in [(f"inputs[{i}]", inp.mount_as)
                         for i, inp in enumerate(self.inputs)] + \
                        ([("code", self.code.mount_as)] if self.code else []):
            if m in mounts:
                raise WeftError(
                    "task.invalid",
                    f"{mounts[m]} and {label} both mount as {m!r}",
                    stage="submit",
                    hints={"path": m,
                           "suggestion": "set mount_as to distinct in-job "
                                         "paths (run+rel inputs default to "
                                         "the source filename)"})
            mounts[m] = label
        seen_out: set[str] = set()
        for o in self.outputs:
            if o in seen_out:
                raise WeftError(
                    "task.invalid", f"output {o!r} is declared twice",
                    stage="submit")
            seen_out.add(o)
            if o in mounts:
                raise WeftError(
                    "task.invalid",
                    f"output {o!r} is also the mount path of {mounts[o]} — "
                    "inputs are read-only (hardlink-shared: writing there "
                    "can falsify the content-addressed record)",
                    stage="submit",
                    hints={"path": o,
                           "suggestion": "write the result to a different "
                                         "declared output path"})
        if self.array is not None and self.array < 1:
            raise WeftError("task.invalid", "array must be >= 1", stage="submit")
        if self.resources.walltime:
            # one grammar (slurm's --time); an unparseable walltime used
            # to crash the POLLER mid-tick instead of refusing at submit
            from .runner_util import walltime_to_s
            walltime_to_s(self.resources.walltime)
        refs = [i.ref for i in self.inputs] + ([self.code.ref] if self.code else [])
        for r in refs:
            if not r.startswith("dref:"):
                raise WeftError(
                    "task.invalid", f"not a DataRef: {r!r}", stage="submit"
                )

    def required_refs(self) -> list[str]:
        refs = [i.ref for i in self.inputs]
        if self.code:
            refs.append(self.code.ref)
        return refs

    def task_hash(self) -> str:
        return task_id(
            {
                "env": self.env,
                "inputs": sorted((i.ref, i.mount_as) for i in self.inputs),
                "code": (self.code.ref, self.code.mount_as) if self.code else None,
                "command": self.command,
                "outputs": sorted(self.outputs),
                "array": self.array,
                "env_vars": self.env_vars,
            }
        )

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "env": self.env,
            "inputs": [{"ref": i.ref, "mount_as": i.mount_as} for i in self.inputs],
            "code": {"ref": self.code.ref, "mount_as": self.code.mount_as}
            if self.code else None,
            "outputs": self.outputs,
            "resources": self.resources.to_dict(),
            "site": self.site,
            "array": self.array,
            "env_vars": self.env_vars,
            "after": self.after,
            "label": self.label,
        }
