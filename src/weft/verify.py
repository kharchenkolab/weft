"""Postcondition verification core (ensure_available P0).

ONE grammar, ONE validator, ONE comparator — consumed by session
verify= and the env-row realize postcondition alike (a verify
vocabulary with two dialects would be the split-brain reborn inside
the feature built after that sweep).

The oracle is honest by construction:
  * site scripts emit weft-authored machine lines (``WEFT-VERIFY
    {json}``) — no free-text parsing of interpreter output, ever;
  * scripts report GOT only; the VERDICT is computed controller-side
    by the one comparator;
  * anything that prevents a check from RUNNING (nonzero rc, timeout,
    missing marker, garbage marker) is ``unknown`` — never "failed",
    never "passed" (fail closed on claiming, fail open on blaming).
"""

from __future__ import annotations

import json
import re

from .errors import WeftError

_VERSION_RE = re.compile(r"(==|>=)\s*([A-Za-z0-9][A-Za-z0-9._-]*)$")
_NAME_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*$")
MARKER = "WEFT-VERIFY "


def validate_verify(v) -> dict:
    """Normalize/refuse a verify request. True -> {} (per-lane
    defaults); False/None -> refused upstream (callers gate on the
    effective verify before building checks); dict -> validated
    {"import": [...], "loads": [...], "versions": {...}}."""
    if v is True:
        return {}
    if not isinstance(v, dict):
        raise WeftError(
            "task.invalid", f"verify must be True or a dict, got {type(v).__name__}",
            stage="realize",
            hints={"grammar": {"import": ["<python module>"],
                               "loads": ["<R package>"],
                               "versions": {"<name>": "==X.Y.Z | >=X.Y"}}})
    unknown = set(v) - {"import", "loads", "versions"}
    if unknown:
        raise WeftError(
            "task.invalid", f"unknown verify keys: {sorted(unknown)}",
            stage="realize",
            hints={"known": ["import", "loads", "versions"]})
    out: dict = {"import": [str(x) for x in v.get("import") or []],
                 "loads": [str(x) for x in v.get("loads") or []],
                 "versions": {str(k): str(s)
                              for k, s in (v.get("versions") or {}).items()}}
    for name in out["import"] + out["loads"] + list(out["versions"]):
        if not _NAME_OK.fullmatch(name):
            raise WeftError(
                "task.invalid", f"not a package/module name: {name!r}",
                stage="realize")
    for name, spec in out["versions"].items():
        if not _VERSION_RE.fullmatch(spec.strip()):
            raise WeftError(
                "task.invalid",
                f"verify.versions[{name!r}] = {spec!r} is not '==X.Y.Z' "
                f"or '>=X.Y'", stage="realize",
                hints={"note": "a postcondition asserts sufficiency — "
                               "only == and >= have meaning here"})
    return out


def usable_want(spec: str) -> bool:
    """Is a dep constraint usable as a verify want? Only the verify
    grammar (==X.Y.Z / >=X.Y, no wildcards) — an unusable want must be
    DROPPED, not passed through: it would land 'unknown' at the
    comparator and record-gating would retract a good install."""
    return bool(_VERSION_RE.fullmatch(spec.strip()))


def _vtuple(s: str) -> tuple | None:
    """Naive comparable form: dashes normalized to dots, numeric parts
    as ints, alpha tails kept as strings. None = incomparable."""
    parts = re.split(r"[.\-]", s.strip())
    out = []
    for p in parts:
        if not p:
            return None
        out.append(int(p) if p.isdigit() else p)
    return tuple(out) if out else None


def compare_versions(got: str, want_spec: str) -> str:
    """'passed' | 'failed' | 'unknown'. The ONE comparator: verdicts
    never come from site-side semantics; incomparable never passes."""
    m = _VERSION_RE.fullmatch(want_spec.strip())
    if not m or not got:
        return "unknown"
    op, want = m.group(1), m.group(2)
    g, w = _vtuple(got), _vtuple(want)
    if g is None or w is None:
        return "unknown"
    if op == "==":
        return "passed" if g == w else "failed"
    # >= : compare element-wise; int-vs-str at a position = incomparable
    for a, b in zip(g, w):
        if type(a) is not type(b):
            return "unknown"
        if a != b:
            return "passed" if a > b else "failed"
    return "passed" if len(g) >= len(w) else "failed"


def default_checks(lane: str, packages: list[str],
                   pins: dict[str, str] | None = None) -> list[dict]:
    """The verify=True reliable defaults. conda/pypi: metadata presence
    by DIST name (dist name != import name — scikit-learn vs sklearn —
    so no import is attempted implicitly). cran: library() load, keyed
    on the RESOLVED name for refs (63b6199). Pins become versions."""
    pins = pins or {}
    kind = {"cran": "loads", "conda": "conda_meta"}.get(lane, "metadata")
    return [{"name": p, "kind": kind, "want": pins.get(p)}
            for p in packages]


def explicit_checks(verify: dict, lane_of: dict[str, str]) -> list[dict]:
    """Explicit verify dict -> check list. `lane_of` maps a versions
    name to its ecosystem so the version GOT is fetched the right way
    (R packageVersion vs python metadata); unmapped names default to
    metadata."""
    checks = [{"name": n, "kind": "import", "want": None}
              for n in verify.get("import", [])]
    checks += [{"name": n, "kind": "loads", "want": None}
               for n in verify.get("loads", [])]
    for n, spec in verify.get("versions", {}).items():
        kind = {"cran": "loads", "conda": "conda_meta"}.get(
            lane_of.get(n), "metadata")
        checks.append({"name": n, "kind": kind, "want": spec})
    return checks


def python_verify_script(checks: list[dict]) -> str:
    """One `python -c` body: a WEFT-VERIFY json line per check; the
    script exits 0 for FAILED checks (failure is marker content —
    nonzero rc means the ORACLE broke, which is unknown, not failed)."""
    payload = json.dumps([{"name": c["name"], "kind": c["kind"]}
                          for c in checks])
    return (
        "import importlib, json, sys\n"
        "import importlib.metadata as md\n"
        f"for c in json.loads({json.dumps(payload)}):\n"
        "    r = {'name': c['name'], 'kind': c['kind']}\n"
        "    try:\n"
        "        if c['kind'] == 'import':\n"
        "            importlib.import_module(c['name'])\n"
        "            r['ok'] = True\n"
        "        else:\n"
        "            r['got'] = md.version(c['name'])\n"
        "            r['ok'] = True\n"
        "    except Exception as e:\n"
        "        r['ok'] = False\n"
        "        r['reason'] = str(e)[:200]\n"
        f"    print({json.dumps(MARKER)} + json.dumps(r))\n"
    )


def r_verify_script(checks: list[dict]) -> str:
    """Rscript body, same marker protocol (library() + packageVersion;
    output is weft-authored — locale cannot perturb it). Package names
    were validated by _NAME_OK (no quotes can reach the vector), and
    are json.dumps-quoted anyway — solvers.py parity."""
    if not checks:
        return ""
    vec = ", ".join(json.dumps(c["name"]) for c in checks)
    return (
        f"for (nm in c({vec})) {{\n"
        "  ok <- tryCatch({ suppressMessages(\n"
        "          library(nm, character.only=TRUE)); TRUE },\n"
        "        error=function(e) FALSE)\n"
        "  got <- tryCatch(as.character(utils::packageVersion(nm)),\n"
        "                  error=function(e) NA_character_)\n"
        "  line <- paste0('" + MARKER + "', '{\"name\": \"', nm,\n"
        "                 '\", \"kind\": \"loads\", \"ok\": ', tolower(ok),\n"
        "                 ifelse(is.na(got), '',\n"
        "                        paste0(', \"got\": \"', got, '\"')), '}')\n"
        "  cat(line, \"\\n\", sep=\"\")\n"
        "}\n")


def sh_conda_verify_script(checks: list[dict]) -> str:
    """Presence oracle for conda packages that are not python dists
    (cmake has no importlib metadata): the conda-meta record IS the
    installed fact. Filename shape <name>-<version>-<build>.json; the
    glob anchors the version at a digit so a prefix-name sibling
    (name- vs name-extra-) cannot match. Names are _NAME_OK-validated
    upstream."""
    lines = []
    for c in checks:
        n = c["name"]
        lines.append(
            f'f=$(ls "$CONDA_PREFIX"/conda-meta/{n}-[0-9]*.json '
            f'2>/dev/null | head -1); '
            f'if [ -n "$f" ]; then v=$(basename "$f" .json); '
            f'v=${{v#{n}-}}; v=${{v%-*}}; '
            f'echo \'{MARKER}\'\'{{"name": "{n}", "kind": "conda_meta", '
            f'"ok": true, "got": "\'"$v"\'"}}\'; '
            f'else echo \'{MARKER}\'\'{{"name": "{n}", '
            f'"kind": "conda_meta", "ok": false, '
            f'"reason": "no conda-meta record"}}\'; fi')
    return "\n".join(lines)


def parse_verify_output(out: str, rc: int,
                        checks: list[dict]) -> dict[str, dict]:
    """Marker lines -> per-check results, fail-closed: rc != 0 -> ALL
    unknown; a check with no (or garbage) marker -> unknown; verdicts
    for versioned checks come from the one comparator."""
    results: dict[str, dict] = {}
    if rc != 0:
        return {c["name"]: {"status": "unknown", "check": c["kind"],
                            "want": c.get("want"),
                            "reason": f"verify oracle exited rc {rc} — "
                                      f"could not run, not failed"}
                for c in checks}
    seen: dict[str, dict] = {}
    for line in out.splitlines():
        if not line.startswith(MARKER):
            continue
        try:
            row = json.loads(line[len(MARKER):])
            seen[row["name"]] = row
        except (ValueError, KeyError, TypeError):
            continue                      # garbage marker = unseen
    for c in checks:
        row = seen.get(c["name"])
        if row is None:
            results[c["name"]] = {"status": "unknown", "check": c["kind"],
                                  "want": c.get("want"),
                                  "reason": "no verify marker emitted"}
            continue
        r = {"check": c["kind"], "want": c.get("want"),
             "got": row.get("got")}
        if not row.get("ok"):
            r["status"] = "failed"
            r["reason"] = row.get("reason", "check reported not-ok")
        elif c.get("want"):
            r["status"] = compare_versions(row.get("got") or "", c["want"])
        else:
            r["status"] = "passed"
        results[c["name"]] = r
    return results


def run_verify(exec_fn, lane: str, checks: list[dict],
               timeout: float = 180.0) -> dict[str, dict]:
    """exec_fn(script, timeout) -> object with .rc/.out — the caller
    supplies the composed-runtime executor (session or realize
    activation). Exceptions from exec_fn (site.unreachable, timeouts)
    are could-not-run: every check is unknown with the typed reason."""
    if not checks:
        return {}
    if lane == "cran":
        script = f"Rscript -e {_shquote(r_verify_script(checks))}"
    elif lane == "conda":
        script = sh_conda_verify_script(checks)     # plain sh, no interp
    else:
        script = f"python -c {_shquote(python_verify_script(checks))}"
    try:
        r = exec_fn(script, timeout)
    except WeftError as e:
        return {c["name"]: {"status": "unknown", "check": c["kind"],
                            "want": c.get("want"),
                            "reason": f"verify oracle could not run: "
                                      f"{e.code}: {e.detail[:160]}"}
                for c in checks}
    return parse_verify_output(r.out or "", r.rc, checks)


def run_grouped(exec_fn, checks: list[dict],
                timeout: float = 180.0) -> dict[str, dict]:
    """Group checks by ORACLE (loads -> R, conda_meta -> sh,
    metadata/import -> python) and run each group — the one grouping
    rule for every consumer (session and realize)."""
    by: dict[str, list] = {"cran": [], "conda": [], "pypi": []}
    for c in checks:
        by[{"loads": "cran", "conda_meta": "conda"}.get(
            c["kind"], "pypi")].append(c)
    out: dict[str, dict] = {}
    for lane, group in by.items():
        out.update(run_verify(exec_fn, lane, group, timeout))
    return out


def _shquote(s: str) -> str:
    import shlex
    return shlex.quote(s)
