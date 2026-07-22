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
from .spec import lane_spelling, split_constraint


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


def probe_lanes(packages: list, lanes: list[str],
                namespace: str) -> dict:
    """{package: {lane: fact}} — the same dialect function the chain
    uses picks each lane's spelling (one derivation, or probe reports a
    false fact for a lane that would succeed)."""
    out: dict = {}
    for pkg in packages:
        if isinstance(pkg, dict):
            display = pkg["name"]
            spellings = {ln: pkg.get(ln) for ln in lanes}
        else:
            display, spellings = pkg, {}
        facts = {}
        for lane in lanes:
            if lane != "cran" and "/" in display.partition("@")[0]:
                facts[lane] = _fact(False, display,
                                    reason="lane grammar cannot speak "
                                           "github refs")
                continue
            sp = spellings.get(lane) or lane_spelling(display, lane,
                                                      namespace)
            facts[lane] = _BACKENDS[lane](split_constraint(sp)[0]
                                          if lane != "cran"
                                          else sp.split()[0])
        out[display] = facts
    return out
