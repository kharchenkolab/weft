"""TransferMethod: bulk bytes between the workspace CAS and a site CAS.

Methods move raw blobs (sha256-named files); refs and manifests stay
controller-side. transfer() must verify at the destination — a method that
returns without error has *proven* the bytes are there and correct.
"""

from __future__ import annotations

from typing import Protocol

from ..cas import LocalCAS


class TransferMethod(Protocol):
    name: str

    def estimate(self, blobs: list[tuple[str, int]], endpoint: dict) -> dict:
        """{"bytes": N, "seconds_guess": float | None}"""
        ...

    def transfer(self, blobs: list[tuple[str, int]], cas: LocalCAS,
                 endpoint: dict, progress=None) -> None:
        """Push blobs from the local CAS to the endpoint CAS. Verifies.
        `progress({"bytes_done": n, ...})` may be called during transfer."""
        ...

    def fetch(self, blobs: list[tuple[str, int]], cas: LocalCAS,
              endpoint: dict, progress=None) -> None:
        """Pull blobs from the endpoint CAS into the local CAS. Verifies."""
        ...
