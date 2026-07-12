"""Structured error taxonomy (design doc 05 §3).

Every failure an agent can see carries (stage, code, detail, hints).
Codes are the stable machine-readable vocabulary; hints carry the
structured payload a specific recovery needs (e.g. job.oom includes
observed peak RSS so the agent can right-size the resubmission).
"""

from __future__ import annotations

from typing import Any

# stage values: solve | realize | staging | submit | queued | running |
#               collecting | infra
CODES = {
    "env.solve_conflict": "unsatisfiable spec; hints list the conflicting requirements",
    "env.layer_conflict": "one dependency layer's requirements contradict another's; hints name both sides and the fix",
    "env.solve_failed": "solver infrastructure failure (network, index); retryable",
    "env.realize_failed": "environment build/unpack failed on site",
    "env.unsatisfiable_on_site": "spec needs something this site lacks; hints list alternative sites",
    "data.transfer_failed": "bulk transfer failed; hints say whether resumable",
    "data.verify_failed": "content hash mismatch after transfer or at use",
    "data.missing": "referenced DataRef unknown or content unavailable",
    "site.unreachable": "control channel to site failed; hints carry backoff schedule",
    "site.capability_violation": "request exceeds site limits; hints give nearest valid ask",
    "site.bootstrap_failed": "shim/pixi installation on site failed",
    "sched.rejected": "scheduler refused the submission",
    "sched.timeout": "scheduler did not respond in time",
    "sched.node_failure": "node died under the job",
    "job.nonzero_exit": "user command failed; hints carry classified log signature",
    "job.oom": "killed for memory; hints carry observed peak vs requested",
    "job.walltime_exceeded": "killed for time; hints carry elapsed vs requested",
    "quota.storage": "site storage quota pressure prevents the operation",
    "budget.exceeded": "operation would exceed a configured spending cap",
    "task.invalid": "task specification is malformed or references unknown objects",
    "state.conflict": "concurrent operation already in progress for this resource",
}


class WeftError(Exception):
    def __init__(
        self,
        code: str,
        detail: str,
        *,
        stage: str = "infra",
        hints: dict[str, Any] | None = None,
        retryable: bool = False,
    ):
        assert code in CODES, f"unknown error code: {code}"
        super().__init__(f"[{code}@{stage}] {detail}")
        self.code = code
        self.stage = stage
        self.detail = detail
        self.hints = hints or {}
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        """Serialization shown to the agent through the tool API."""
        return {
            "error": self.code,
            "stage": self.stage,
            "detail": self.detail,
            "retryable": self.retryable,
            "hints": self.hints,
            "meaning": CODES[self.code],
        }
