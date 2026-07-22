"""Source fetchers: ingest remote artifacts into the data plane (A2).

Ingest, not cataloging: weft fetches, hashes, verifies, and records —
discovery and metadata of the remote repository stay above. Registry
mirrors transfers/solvers: one fetcher per scheme family; unknown schemes
fail fast listing what's registered.

With `site=`, bytes go straight into that site's CAS (fetched and hashed
site-side) and never detour through the controller — the ref exists only
there, like task outputs. Identity note: site-direct ingest names blobs by
plain sha256 regardless of size (no site-side chunking), so a >64 MiB
URL ingest won't dedup against a locally-registered copy of the same
file; `meta.trust` records "verified" (expected hash matched) or
"first-fetch" (hash-on-arrival is the identity).
"""

from __future__ import annotations

import hashlib
import shlex
import urllib.error
import urllib.request
from pathlib import Path

from .errors import WeftError


class HttpFetcher:
    schemes = ("http", "https")

    def fetch_to_file(self, url: str, dest: Path) -> str:
        """Stream to dest, hashing on the way; -> sha256."""
        h = hashlib.sha256()
        req = urllib.request.Request(url, headers={"User-Agent": "weft"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r, \
                    open(dest, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    h.update(chunk)
                    f.write(chunk)
        except urllib.error.HTTPError as e:
            # 4xx is the SERVER'S final answer about this URL — retrying
            # a 404 forever is not a strategy (429/5xx stay retryable)
            raise WeftError(
                "data.transfer_failed", f"fetch failed: {url}",
                stage="staging",
                retryable=e.code >= 500 or e.code == 429,
                hints={"source": url, "http_status": e.code,
                       "detail": str(e)[-300:]},
            ) from e
        except Exception as e:
            raise WeftError(
                "data.transfer_failed", f"fetch failed: {url}",
                stage="staging", retryable=True,
                hints={"source": url, "detail": str(e)[-300:]},
            ) from e
        return h.hexdigest()

    def fetch_on_site(self, adapter, url: str, dest_abs: str) -> None:
        r = adapter.run_cmd(
            f"curl -fsSL -o {shlex.quote(dest_abs)} {shlex.quote(url)} || "
            f"wget -q -O {shlex.quote(dest_abs)} {shlex.quote(url)}",
            timeout=3600,
        )
        if r.rc != 0:
            raise WeftError(
                "data.transfer_failed",
                f"site-side fetch failed on {adapter.name}",
                stage="staging", retryable=True,
                hints={"source": url, "detail": (r.err or r.out)[-300:],
                       "note": "the site needs curl or wget and outbound "
                               "access; omit site= to fetch via the "
                               "controller instead"},
            )


class RcloneFetcher(HttpFetcher):
    """Object stores via rclone (s3/gs/azure/…): optional static binary,
    same pattern as pixi-pack. Controller-side only in v1."""

    schemes = ("s3", "gs", "azure")

    def __init__(self, rclone_bin: str | None):
        self.rclone = rclone_bin

    def fetch_to_file(self, url: str, dest: Path) -> str:
        import subprocess
        if not self.rclone:
            raise WeftError(
                "data.transfer_failed",
                "object-store ingest needs the rclone binary",
                stage="staging",
                hints={"suggestion": "install rclone next to pixi "
                                     "(.env/bin/rclone) or use an https URL"},
            )
        r = subprocess.run(
            [self.rclone, "copyto", url.replace("s3://", ":s3:", 1)
             .replace("gs://", ":gcs:", 1).replace("azure://", ":azureblob:", 1),
             str(dest)],
            capture_output=True, text=True, timeout=3600)
        if r.returncode != 0 or not dest.exists():
            raise WeftError(
                "data.transfer_failed", f"rclone fetch failed: {url}",
                stage="staging", retryable=True,
                hints={"detail": r.stderr[-300:]})
        h = hashlib.sha256()
        with open(dest, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def fetch_on_site(self, adapter, url, dest_abs):
        raise WeftError(
            "data.transfer_failed",
            "site-direct object-store ingest is not supported yet",
            stage="staging",
            hints={"suggestion": "omit site= (controller fetch) or use an "
                                 "https URL the site can curl"})


def default_fetchers(rclone_bin: str | None = None) -> dict:
    http = HttpFetcher()
    rc = RcloneFetcher(rclone_bin)
    return {s: http for s in http.schemes} | {s: rc for s in rc.schemes}
