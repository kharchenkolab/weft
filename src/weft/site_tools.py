"""Controller-side acquisition of site tooling (pixi, pixi-unpack).

A site needs these binaries built FOR ITS OWN platform; the controller's
copies only help when the platforms match (the historical all-linux
setup). On mismatch — a darwin controller driving a linux cluster, an
aarch64 container fixture — the pinned release is fetched once per
(tool, version, platform) into a shared controller cache and pushed like
any other bootstrap file. The site never needs its own network for this.

Downloads are https + release-pinned; pixi assets additionally ship a
`.sha256` companion which is verified when present (pixi-pack publishes
none — those rely on the pinned tag over TLS).
"""

from __future__ import annotations

import hashlib
import os
import urllib.error
import urllib.request
from pathlib import Path

from .errors import WeftError

PIXI_VERSION = "v0.72.2"
PIXI_UNPACK_VERSION = "v0.7.10"

# conda subdir -> rust release triple (musl: one binary per arch covers
# every glibc vintage a cluster might run, including el7)
_TRIPLES = {
    "linux-64": "x86_64-unknown-linux-musl",
    "linux-aarch64": "aarch64-unknown-linux-musl",
    "osx-arm64": "aarch64-apple-darwin",
    "osx-64": "x86_64-apple-darwin",
}

# tool -> (github repo, pinned version, version override env var)
_TOOLS = {
    "pixi": ("prefix-dev/pixi", PIXI_VERSION, "WEFT_PIXI_VERSION"),
    "pixi-unpack": ("Quantco/pixi-pack", PIXI_UNPACK_VERSION,
                    "WEFT_PIXI_UNPACK_VERSION"),
    # pixi-pack runs on the CONTROLLER (packed-strategy builds), fetched
    # for the controller's own platform when no sibling binary exists
    "pixi-pack": ("Quantco/pixi-pack", PIXI_UNPACK_VERSION,
                  "WEFT_PIXI_UNPACK_VERSION"),
}


def _cache_root() -> Path:
    return Path(os.environ.get(
        "WEFT_SITE_TOOLS_CACHE",
        str(Path.home() / ".cache" / "weft" / "site-tools")))


def _download(url: str, timeout: float = 120.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "weft"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_tool(tool: str, platform: str) -> Path:
    """Return a controller-cached binary of `tool` for `platform`,
    downloading the pinned release on first use."""
    if tool not in _TOOLS:
        raise WeftError("task.invalid", f"unknown site tool: {tool}",
                        stage="infra")
    repo, version, env_key = _TOOLS[tool]
    version = os.environ.get(env_key, version)
    triple = _TRIPLES.get(platform)
    if triple is None:
        raise WeftError(
            "site.bootstrap_failed",
            f"no {tool} build for platform {platform}", stage="infra",
            hints={"known_platforms": sorted(_TRIPLES)})
    dest = _cache_root() / tool / version / platform / tool
    if dest.exists():
        return dest
    url = (f"https://github.com/{repo}/releases/download/"
           f"{version}/{tool}-{triple}")
    try:
        data = _download(url)
    except (urllib.error.URLError, OSError) as e:
        raise WeftError(
            "site.bootstrap_failed",
            f"could not fetch {tool} {version} for {platform}",
            stage="infra", retryable=True,
            hints={"url": url, "cause": str(e)[:200],
                   "suggestion": f"place a {platform} {tool} binary at "
                                 f"{dest} yourself, or pass pixi_source "
                                 f"pointing at one"})
    try:  # integrity companion: published for pixi, absent for pixi-pack
        want = _download(url + ".sha256").decode().split()[0].strip()
        got = hashlib.sha256(data).hexdigest()
        if got != want:
            raise WeftError(
                "data.verify_failed",
                f"{tool} download hash mismatch", stage="infra",
                retryable=True, hints={"url": url, "expected": want,
                                       "got": got})
    except (urllib.error.URLError, OSError):
        pass
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(f".tmp.{os.getpid()}")  # concurrent-fetch safe
    tmp.write_bytes(data)
    tmp.chmod(0o755)
    tmp.rename(dest)  # atomic: losers overwrite with identical bytes
    return dest


def ensure_site_tools(adapter, site_platform: str) -> dict:
    """Make bin/pixi and bin/pixi-unpack on the site real *for its
    platform*. A present-but-unrunnable binary (e.g. a darwin pixi pushed
    to a linux site by a platform-blind bootstrap) is replaced. Best
    effort per tool; the report says what happened."""
    report = {}
    for tool in ("pixi", "pixi-unpack"):
        rel = f"bin/{tool}"
        if adapter.file_exists(rel):
            r = adapter.run_cmd(
                f"{adapter.path(rel)} --version >/dev/null 2>&1 && echo ok",
                timeout=30)
            if "ok" in r.out:
                report[tool] = "ok"
                continue
        try:
            local = fetch_tool(tool, site_platform)
            adapter._push_binary(local, rel)
            r = adapter.run_cmd(
                f"{adapter.path(rel)} --version 2>&1 | head -1", timeout=30)
            report[tool] = (f"pushed for {site_platform}: {r.out.strip()}"
                            if r.rc == 0 else
                            f"pushed but not runnable: {r.out.strip()[:120]}")
        except WeftError as e:
            report[tool] = f"unavailable: {e.detail}"
    return report
