"""A deterministic doctrine-following agent (doc 05 §7, doc 06 §2).

This is the evaluation harness's stand-in for the LLM: it reads only what
a real agent would read — the structured error (code + hints) — and applies
the documented remediation. If the taxonomy and hints are good enough for
this 80-line policy table to recover, they give a language model every
chance; if a hint's wording sends *this* agent into a loop, it would have
misled the model too. Scoring: recovery rate, rounds used, and the
unchanged-resubmission count (doctrine: never resubmit an unchanged failing
task more than once — here, never at all).
"""

from __future__ import annotations

import copy
import re
import time


class ScriptedAgent:
    def __init__(self, weft, max_rounds: int = 5):
        self.w = weft
        self.max_rounds = max_rounds

    def run(self, task: dict, spec: dict | None = None) -> dict:
        """Drive one task to completion, remediating per the error taxonomy."""
        task = copy.deepcopy(task)
        actions: list[str] = []
        submitted_hashes: list[str] = []
        unchanged_resubmits = 0

        for round_no in range(1, self.max_rounds + 1):
            if spec is not None:
                ensured = self.w.env_ensure(spec)
                if "error" in ensured:
                    fixed = self._fix_spec(ensured, spec, actions)
                    if not fixed:
                        return self._result(False, round_no, actions,
                                            unchanged_resubmits, ensured)
                    continue
                task["env"] = ensured["env_id"]

            r = self.w.task_submit(task, force=bool(submitted_hashes))
            if "error" in r:
                if not self._fix_submit_error(r, task, actions):
                    return self._result(False, round_no, actions,
                                        unchanged_resubmits, r)
                continue

            fingerprint = repr(sorted(task.items())) + repr(spec)
            if fingerprint in submitted_hashes:
                unchanged_resubmits += 1
            submitted_hashes.append(fingerprint)

            job = self.w.runner.wait(r["job_id"], 300)
            if job["state"] == "DONE":
                return self._result(True, round_no, actions,
                                    unchanged_resubmits, job["manifest"])
            err = job["error"] or {}
            if not self._fix_job_error(err, task, actions):
                return self._result(False, round_no, actions,
                                    unchanged_resubmits, err)
        return self._result(False, self.max_rounds, actions,
                            unchanged_resubmits, {"note": "round budget spent"})

    # -- remediation policies (exactly what the hints advertise) -----------

    def _fix_spec(self, err: dict, spec: dict, actions: list[str]) -> bool:
        if err["error"] != "env.solve_conflict":
            return False
        # relax the pin the solver named (hints carry user_pins + message)
        message = err["hints"].get("solver_message", "")
        for pin in err["hints"].get("user_pins", []):
            name = pin.split()[0]
            if name in message and " " in pin:
                deps = spec["deps"]["conda"]
                deps[deps.index(pin)] = name  # drop the version constraint
                actions.append(f"relaxed pin: {pin!r} -> {name!r}")
                return True
        return False

    def _fix_submit_error(self, err: dict, task: dict, actions: list[str]) -> bool:
        code = err["error"]
        if code == "site.capability_violation":
            for res, h in err["hints"].items():
                if isinstance(h, dict) and "max" in h and "asked" in h:
                    task.setdefault("resources", {})[res] = h["max"]
                    actions.append(f"clamped {res} to site max {h['max']}")
                    return True
        if code == "env.unsatisfiable_on_site":
            others = [s["name"] for s in self.w.sites_list()
                      if s["name"] != task.get("site")]
            if others:
                task["site"] = others[0]
                actions.append(f"re-placed to {others[0]}")
                return True
        return False

    def _fix_job_error(self, err: dict, task: dict, actions: list[str]) -> bool:
        code = err.get("error")
        hints = err.get("hints", {})
        res = task.setdefault("resources", {})
        if code == "job.oom":
            peak = hints.get("observed_peak_gb") or 0
            asked = hints.get("requested_gb") or res.get("mem_gb") or 1
            res["mem_gb"] = max(int(peak * 1.5) + 1, asked * 2)
            actions.append(f"raised mem_gb to {res['mem_gb']} "
                           f"(peak was {peak}, asked {asked})")
            return True
        if code == "job.walltime_exceeded":
            current = hints.get("walltime_s") or 60
            new = int(current * 2)
            res["walltime"] = time.strftime("%H:%M:%S", time.gmtime(new))
            actions.append(f"doubled walltime to {res['walltime']}")
            return True
        if code in ("data.verify_failed", "site.unreachable") and err.get("retryable"):
            actions.append(f"retryable {code}: resubmitting as instructed")
            return True
        if code == "env.unsatisfiable_on_site":
            return self._fix_submit_error({"error": code, "hints": hints},
                                          task, actions)
        return False

    @staticmethod
    def _result(success, rounds, actions, unchanged, last) -> dict:
        return {"success": success, "rounds": rounds, "actions": actions,
                "unchanged_resubmits": unchanged, "last": last}
