"""Preview generation: turn output files into the relevant kilobyte.

The agent reasons over previews to decide what to fetch in full (doc 04 §5).
Generation is heuristic by extension + content sniffing, bounded in size,
and never fails a job — a preview error degrades to a stat-only entry.
"""

from __future__ import annotations

import base64
import json
import struct

INLINE_JSON_CAP = 16 * 1024
TEXT_HEAD_LINES = 20
IMAGE_EMBED_CAP = 32 * 1024

_TEXT_EXT = {".txt", ".log", ".md", ".yaml", ".yml", ".toml", ".cfg", ".ini",
             ".dat", ".out", ".err", ".py", ".sh", ".json", ".csv", ".tsv"}


def _is_probably_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _png_dims(head: bytes) -> tuple[int, int] | None:
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
        w, h = struct.unpack(">II", head[16:24])
        return w, h
    return None


def preview_for(name: str, head: bytes, size: int, full_available: bool = True) -> dict:
    """Build a preview dict from a file's head bytes (site-side fetch cap).

    `head` is whatever prefix of the file the caller could get cheaply;
    `size` is the true full size.
    """
    lower = name.lower()
    ext = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""

    try:
        if ext == ".json" and size <= INLINE_JSON_CAP and len(head) >= size:
            try:
                return {"kind": "inline-json", "value": json.loads(head.decode())}
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        if ext == ".png":
            dims = _png_dims(head)
            out = {"kind": "image", "format": "png"}
            if dims:
                out["width"], out["height"] = dims
            if size <= IMAGE_EMBED_CAP and len(head) >= size:
                out["png_b64"] = base64.b64encode(head).decode()
            return out

        if ext in (".csv", ".tsv"):
            text = head.decode("utf-8", "replace")
            lines = text.splitlines()
            complete = len(head) >= size
            return {
                "kind": "table-head",
                "delimiter": "\t" if ext == ".tsv" else ",",
                "header": lines[0] if lines else "",
                "rows": lines[1 : 1 + min(10, len(lines) - 1)],
                "approx_rows": (len(lines) - 1) if complete else None,
            }

        if lower.endswith((".h5", ".hdf5")):
            return {"kind": "hdf5", "detail": "binary HDF5; fetch to inspect structure"}

        if ext in _TEXT_EXT or _is_probably_text(head[:1024]):
            text = head.decode("utf-8", "replace")
            lines = text.splitlines()
            return {
                "kind": "text-head",
                "lines": lines[:TEXT_HEAD_LINES],
                "truncated": (len(head) < size) or len(lines) > TEXT_HEAD_LINES,
            }
    except Exception:
        pass

    return {"kind": "binary", "magic": head[:8].hex()}
