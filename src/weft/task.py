"""Task model: the pure execution request, content-hashable for memoization.

TaskID covers what determines the *result*: environment identity, input
refs and their mount points, code, command, declared outputs, array shape,
env vars. Site and resources are deliberately excluded — the same task on
a different site or with a bigger memory ask is the same computation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import WeftError
from .ids import task_id


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

    def to_dict(self) -> dict:
        return {"cpus": self.cpus, "mem_gb": self.mem_gb, "gpus": self.gpus,
                "walltime": self.walltime}


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

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        d = dict(d.get("task", d))
        unknown = set(d) - {
            "command", "env", "inputs", "code", "outputs", "resources",
            "site", "array", "env_vars",
        }
        if unknown:
            raise WeftError(
                "task.invalid", f"unknown task fields: {sorted(unknown)}",
                stage="submit",
                hints={"known_fields": [
                    "command", "env", "inputs", "code", "outputs",
                    "resources", "site", "array", "env_vars"]},
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
            ),
            site=d.get("site", "auto"),
            array=int(d["array"]) if d.get("array") else None,
            env_vars={k: str(v) for k, v in (d.get("env_vars") or {}).items()},
        )
        t.validate()
        return t

    def validate(self) -> None:
        for mount in [i.mount_as for i in self.inputs] + (
            [self.code.mount_as] if self.code else []
        ) + list(self.outputs):
            if mount.startswith("/") or ".." in mount.split("/"):
                raise WeftError(
                    "task.invalid",
                    f"paths must be sandbox-relative without '..': {mount!r}",
                    stage="submit",
                )
        if self.array is not None and self.array < 1:
            raise WeftError("task.invalid", "array must be >= 1", stage="submit")
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
        }
