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


class LadderAgent:
    """The CORNER CENSUS agent (the generalization's dynamic guard): an
    agent that provisions/repairs an env reading ONLY structured
    outputs — env_inspect facts, refusal hints, result notes — and
    descends the escalation ladder (inspect -> lever -> env_exec
    diagnose -> env_amend/session repair). The property under test is
    that no lever chain is a DEAD END or a LOOP: every corner resolves
    to success OR terminates at an HONEST 'cannot, because X' (a
    refusal that advertises no further lever), and a (code, subject)
    pair never repeats — a repeat is a cycle, which is the bug this
    exists to catch. If this table navigates the hints, a language
    model has every chance; if a hint loops THIS agent, it would have
    misled the model too."""

    def __init__(self, weft, max_steps: int = 6):
        self.w = weft
        self.max_steps = max_steps

    def provision(self, env_id: str, site: str,
                  needs: str | None = None,
                  repair_cmd: str | None = None) -> dict:
        """Reach a usable env. `needs`: a predicate name env_inspect can
        answer ('pip'); `repair_cmd`: the fix to apply if it is absent.
        Returns {resolved, terminal_honest, chain, seen, env_id}."""
        chain: list[str] = []
        seen: list[tuple] = []       # (code, subject) — a repeat is a loop
        eid = env_id
        for _ in range(self.max_steps):
            insp = self.w.env_inspect(eid, site)
            if not insp.get("realized"):
                # lever: env_status/env_realize (named in the suggestion)
                sug = insp.get("suggestion", "")
                if "env_realize" not in sug:
                    return self._term(chain, seen, honest=False, eid=eid)
                chain.append("env_realize (from inspect suggestion)")
                self.w.env_realize(eid, site)
                continue
            itp = insp.get("interpreter") or {}
            if needs == "pip" and not itp.get("pip_version") \
                    and repair_cmd:
                # the fix an ssh agent would make — but in-band + re-owned
                r = self.w.env_amend(eid, site, repair_cmd,
                                     why="corner-census repair")
                if "error" not in r:
                    chain.append(f"env_amend -> {r['env_id']}")
                    return {"resolved": True, "terminal_honest": False,
                            "chain": chain, "seen": seen,
                            "env_id": r["env_id"]}
                # refusal: follow the posted door, guarding against loops
                key = (r["error"], eid)
                if key in seen:
                    return self._term(chain, seen, honest=False, eid=eid,
                                      note="LOOP: lever led back to the "
                                           "same refusal")
                seen.append(key)
                door = self._door(r)
                if door is None:
                    # honest dead-end: refusal names no further lever
                    return self._term(chain, seen, honest=True, eid=eid)
                verb, arg = door
                chain.append(f"{r['error']} -> {verb}")
                if verb == "extends_env":
                    got = self.w.env_ensure(
                        {"extends_env": eid, "deps": {}})
                    if "error" in got:
                        return self._term(chain, seen, honest=False,
                                          eid=eid)
                    eid = got["env_id"]
                    self.w.env_realize(eid, site)
                    continue
                return self._term(chain, seen, honest=True, eid=eid)
            # nothing to repair — the env is usable as-is
            return {"resolved": True, "terminal_honest": False,
                    "chain": chain, "seen": seen, "env_id": eid}
        return self._term(chain, seen, honest=False, eid=eid,
                          note="STEP BUDGET SPENT — chain did not "
                               "terminate")

    @staticmethod
    def _door(err: dict):
        """Extract the FIRST actionable lever a refusal advertises. The
        contract: a wall posts its door. No door found -> honest
        dead-end (acceptable); a door that exists -> must be live."""
        h = err.get("hints") or {}
        levers = h.get("levers") or h.get("options") or {}
        for k, v in levers.items():
            if "extends_env" in k or "extends_env" in str(v):
                return ("extends_env", v)
            if "session" in k:
                return ("session", v)
        return None

    @staticmethod
    def _term(chain, seen, *, honest, eid, note=None):
        out = {"resolved": False, "terminal_honest": honest,
               "chain": chain, "seen": seen, "env_id": eid}
        if note:
            out["note"] = note
        return out
