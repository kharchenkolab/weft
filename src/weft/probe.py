"""ensure_available probe backends (P5): per-lane availability FACTS
for ranking decisions — observation, never choice, never mutation.

Honesty rules: a 404 from the index is available:false (the server's
answer); ANY transport/parse failure is available:"unknown" with the
typed reason — unknown is never false (an agent ranking on a false
fact is the failure mode this exists to prevent). Every query echoes
the SPELLING it asked about (dialect observability)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .errors import WeftError
from .spec import lane_spellings, split_constraint


def _get_json(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"User-Agent": "weft"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _fact(available, spelling, version=None, reason=None) -> dict:
    out = {"available": available, "spelling": spelling}
    if version:
        out["version_latest"] = version
    if reason:
        out["reason"] = reason
    return out


def probe_pypi(name: str) -> dict:
    """PyPI JSON API — the index's own answer."""
    try:
        data = _get_json(f"https://pypi.org/pypi/{name}/json")
        return _fact(True, name, data.get("info", {}).get("version"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return _fact(False, name)
        return _fact("unknown", name,
                     reason=f"pypi api http {e.code}")
    except Exception as e:
        return _fact("unknown", name, reason=str(e)[-160:])


def probe_conda(name: str, channel: str = "conda-forge") -> dict:
    """anaconda.org package API for the channel."""
    try:
        data = _get_json(
            f"https://api.anaconda.org/package/{channel}/{name}")
        return _fact(True, name, data.get("latest_version"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return _fact(False, name)
        return _fact("unknown", name,
                     reason=f"anaconda api http {e.code}")
    except Exception as e:
        return _fact("unknown", name, reason=str(e)[-160:])


def probe_cran(name: str) -> dict:
    """crandb JSON API (the registry's mirror-of-record metadata)."""
    try:
        data = _get_json(f"https://crandb.r-pkg.org/{name}")
        return _fact(True, name, data.get("Version"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return _fact(False, name)
        return _fact("unknown", name,
                     reason=f"crandb api http {e.code}")
    except Exception as e:
        return _fact("unknown", name, reason=str(e)[-160:])


_BACKENDS = {"pypi": probe_pypi, "conda": probe_conda, "cran": probe_cran}


def _probe_candidates(lane: str, cands: list[str]) -> dict:
    """One fact from a RANKED candidate list (the dialect function may
    yield several spellings — r-x, then bioconductor-x): the first
    spelling the index KNOWS answers; a miss carries every spelling
    tried, so the fact names its own search. Honesty rule preserved
    across candidates: if any candidate answered UNKNOWN, the fact is
    unknown, never false. bioconductor-* spellings query the bioconda
    channel — that is where those builds live; asking conda-forge
    would 404 every real one."""
    tried: list[str] = []
    facts: list[dict] = []
    for sp in cands:
        n = split_constraint(sp)[0]
        tried.append(n)
        if lane == "conda" and n.startswith("bioconductor-"):
            f = probe_conda(n, channel="bioconda")
        else:
            f = _BACKENDS[lane](n)
        facts.append(f)
        if f["available"] is True:
            break
    out = facts[-1]
    unknown = next((f for f in facts if f["available"] == "unknown"), None)
    if out["available"] is not True and unknown is not None:
        out = unknown
    if out["available"] is not True and len(tried) > 1:
        out = {**out, "tried": tried}
    return out


def probe_lanes(packages: list, lanes: list[str],
                namespace: str,
                cran_repos: list | None = None) -> dict:
    """{package: {lane: fact}} — the same dialect function the chain
    uses picks each lane's spelling (one derivation, or probe reports a
    false fact for a lane that would succeed). With extra cran_repos,
    the cran lane answers UNKNOWN: crandb indexes CRAN only, and a
    package living in a secondary registry would otherwise probe FALSE
    — a lie an agent would rank on."""
    out: dict = {}
    for pkg in packages:
        if isinstance(pkg, dict):
            display = pkg["name"]
            spellings = {ln: pkg.get(ln) for ln in lanes}
        else:
            display, spellings = pkg, {}
        facts = {}
        for lane in lanes:
            if "/" in display.partition("@")[0]:
                if lane == "cran":
                    # a github ref is INSTALLABLE on the cran lane but
                    # not probeable against crandb — probing the literal
                    # "owner/repo" 404'd and reported FALSE, a lie an
                    # agent ranks on (parser-sweep find #3)
                    facts[lane] = _fact(
                        "unknown", display,
                        reason="github refs resolve at solve time "
                               "(crandb indexes CRAN names only) — "
                               "unknown, never false")
                else:
                    facts[lane] = _fact(False, display,
                                        reason="lane grammar cannot speak "
                                               "github refs")
                continue
            ov = spellings.get(lane)
            cands = [ov] if ov else lane_spellings(display, lane,
                                                   namespace)
            if lane == "cran":
                # ONE parser for the cran grammar: the old sp.split()[0]
                # passed "Matrix==1.6" (no space) whole to crandb -> 404
                # -> FALSE for a package that exists
                from .spec import parse_cran_dep
                cran_name = parse_cran_dep(cands[0])["name"]
                if cran_repos:
                    facts[lane] = _fact(
                        "unknown", cran_name,
                        reason="secondary repositories are not probeable "
                               "(crandb indexes CRAN only) — unknown, "
                               "never false")
                    continue
                facts[lane] = _BACKENDS[lane](cran_name)
                continue
            facts[lane] = _probe_candidates(lane, cands)
        out[display] = facts
    return out
