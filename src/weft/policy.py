"""Per-site user policy: rules the owner of the account sets (doc 02 §2).

Two halves, handled differently on purpose:

  * structured, machine-checkable knobs — weft *enforces* them at submit
    time and in adapter behavior (partition allowlist, GPU cap, concurrent
    job cap, storage roles);
  * free-form guidance (`notes`) — weft cannot check "don't use during the
    day", so it *surfaces* the notes to the agent in site descriptions and
    in every submit plan, where they can steer planning.

Schema (all fields optional):

    policy:
      partitions_allowed: ["standard", "short"]
      max_gpus: 4
      max_concurrent_jobs: 50
      storage:
        large: /groups/phys/me      # big persistent datasets
        scratch: /scratch/me        # fast cluster-wide temp
        node_tmp: /tmp              # node-local scratch
      notes:
        - "prefer nights/weekends for >1h jobs"
        - "group quota on /groups is shared — clean up after campaigns"
"""

from __future__ import annotations

from .errors import WeftError


def site_policy(site_row: dict | None) -> dict:
    return ((site_row or {}).get("config") or {}).get("policy") or {}


def enforce_policy(
    policy: dict, resources: dict, active_jobs_at_site: int, site: str,
) -> None:
    """Raise site.capability_violation when a structured rule is broken.

    The hints always name the rule and its source ("site policy set by the
    user"), so the agent knows this is a *choice* to respect, not a
    hardware limit to negotiate with.
    """
    allowed = policy.get("partitions_allowed")
    if allowed and resources.get("partition") \
            and resources["partition"] not in allowed:
        raise WeftError(
            "site.capability_violation",
            f"partition {resources['partition']!r} is outside the user's "
            f"allowlist on {site}",
            stage="submit",
            hints={"rule": "partitions_allowed", "allowed": allowed,
                   "source": "site policy set by the user",
                   "suggestion": f"pick one of {allowed} or ask the user "
                                 "to widen the allowlist"},
        )
    max_gpus = policy.get("max_gpus")
    if max_gpus is not None and resources.get("gpus", 0) > max_gpus:
        raise WeftError(
            "site.capability_violation",
            f"user policy on {site} caps GPUs at {max_gpus}",
            stage="submit",
            hints={"rule": "max_gpus", "limit": max_gpus,
                   "asked": resources.get("gpus"),
                   "source": "site policy set by the user",
                   "suggestion": f"resubmit with gpus <= {max_gpus} or ask "
                                 "the user to relax the policy"},
        )
    cap = policy.get("max_concurrent_jobs")
    if cap is not None and active_jobs_at_site >= cap:
        raise WeftError(
            "site.capability_violation",
            f"user policy on {site} caps concurrent jobs at {cap} "
            f"({active_jobs_at_site} active)",
            stage="submit",
            hints={"rule": "max_concurrent_jobs", "limit": cap,
                   "active": active_jobs_at_site,
                   "source": "site policy set by the user",
                   "suggestion": "wait for jobs to finish or use another site"},
        )


def allowed_partition(policy: dict, configured: str | None, site: str) -> str | None:
    """Reconcile the adapter's partition with the policy allowlist."""
    allowed = policy.get("partitions_allowed")
    if not allowed:
        return configured
    if configured is None:
        return allowed[0]
    if configured not in allowed:
        raise WeftError(
            "site.capability_violation",
            f"partition {configured!r} is outside the user's allowlist on {site}",
            stage="submit",
            hints={"rule": "partitions_allowed", "allowed": allowed,
                   "source": "site policy set by the user"},
        )
    return configured


def storage_env_vars(policy: dict) -> dict[str, str]:
    """Storage roles become guaranteed env vars inside every job sandbox,
    so agent-written commands can target the right filesystem tier."""
    storage = policy.get("storage") or {}
    out = {}
    for role, var in (("large", "WEFT_STORAGE_LARGE"),
                      ("scratch", "WEFT_STORAGE_SCRATCH"),
                      ("node_tmp", "WEFT_STORAGE_NODE_TMP")):
        if storage.get(role):
            out[var] = str(storage[role])
    return out
