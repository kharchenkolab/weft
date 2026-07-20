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
    "env.not_realized": "environment exists but is not realized on this site yet; realize it (run a task with it) first",
    "env.unsatisfiable_on_site": "spec needs something this site lacks; hints list alternative sites",
    "env.platform_mismatch": "environment locked for platforms that do not include this site's; hints name both and the spec fix",
    "env.evict_blocked": "overlay environments stack on this prefix; hints name them and the cascade lever",
    "retain.no_durable": "the site declares no durable storage; hints carry the levers (dest='@workspace', durable=true, durable='/path')",
    "session.cold_base": "the session's base was adopted/unpacked here (cold package cache): cloning it would re-download the whole base; hints carry the levers (extends_env, warm-cache site, full_clone=true)",
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
    "task.dep_failed": "an upstream dependency failed or vanished; this job never started",
    "state.conflict": "concurrent operation already in progress for this resource",
    "internal.error": "unexpected internal failure (a weft bug, not a known failure mode); a retry may or may not help — worth reporting",
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
