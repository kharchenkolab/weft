"""MCP server for weft: the tool surface over stdio JSON-RPC.

Minimal, dependency-free implementation of the MCP subset that matters
(initialize / tools/list / tools/call). Tool schemas are generated from
the `Weft` method signatures and docstrings — `api.PUBLIC_TOOLS` is the
single source of truth, and the uniform returns-never-raises contract
means every call maps 1:1 onto a JSON result.

Run:  python -m weft.mcp_server --workspace /path/to/project \
          [--pixi-bin /path/to/pixi]
"""

from __future__ import annotations

import inspect
import json
import sys
import typing


def _json_type(annotation) -> dict:
    origin = typing.get_origin(annotation)
    if annotation in (int, float):
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is str:
        return {"type": "string"}
    if origin in (list, tuple) or annotation in (list, tuple):
        return {"type": "array"}
    if origin is dict or annotation is dict:
        return {"type": "object"}
    if origin is typing.Union:  # Optional[X] and unions: accept any member
        members = [m for m in typing.get_args(annotation)
                   if m is not type(None)]
        return _json_type(members[0]) if len(members) == 1 else {}
    return {}


# property-level schema detail the type system can't express: the shapes
# a cold agent must be able to discover from tools/list alone
SCHEMA_HINTS: dict[str, dict] = {
    "task_submit": {"task": {
        "type": "object",
        "description": (
            'The task: {"command": str (required), "env": EnvID|null, '
            '"inputs": [{"ref": "dref:…", "mount_as": "rel/path"}], '
            '"code": {ref, mount_as}, "outputs": ["results/"], '
            '"resources": {"cpus", "mem_gb", "gpus", "walltime"}, '
            '"site": name|"auto", "array": N, "env_vars": {k: v}}'),
    }},
    "env_ensure": {"spec_or_id": {
        "description": (
            'An EnvID string, or a spec: {"name", "deps": {"conda": [...], '
            '"pypi": [...], "cran": [...]}, "modules": [...], '
            '"env_vars": {...}, "extends": spec-hash, '
            '"system_requirements": {"cuda", "cran_snapshot"}}'),
    }},
    "register_site": {"config": {
        "description": ('Site config: {"root": path, "pixi_source": path, '
                        'ssh: +{"host","port","user","ssh_opts"}, slurm: '
                        '+{"scheduler","modules_init"}, "policy": {...}}'),
    }},
}

# fallback descriptions for thin wrappers (docstrings win when present)
TOOL_DESCRIPTIONS: dict[str, str] = {
    "sites_list": "Registered sites: health, capabilities headline, policy.",
    "sites_describe": "Full capability record + config for one site.",
    "site_probe": "Re-probe a site's capabilities (drift check).",
    "env_status": "Env summary + per-site realizations (with failure logs).",
    "env_repair": "Clear a corrupt realization; next task rebuilds it.",
    "data_register": "Hash a workspace file/dir into a DataRef.",
    "data_describe": "DataRef metadata, size, and known site locations.",
    "data_fetch": "Bring a ref's content back to the workspace (verified).",
    "task_status": "Job states (list); QUEUED entries carry queue_reason.",
    "task_result": "The manifest (outputs with previews) or the error.",
    "task_cancel": "Cancel a queued or running job.",
    "array_status": "Group digest + per-element states (memoized marked).",
    "array_result": "Group roll-up: wall stats, failures, output bytes.",
    "reconcile": "After a controller restart: resume watching all jobs.",
    "session_start": "Mutable scratch clone of a realized env (exploration).",
    "session_exec": "Run a command inside the session env.",
    "session_install": "Incrementally add conda/pypi packages to a session.",
    "session_snapshot": "Freeze session additions into a real, citable EnvID.",
    "session_stop": "Discard a session environment.",
    "kernel_start": "Start a persistent interpreter (python/r/julia) on a "
                    "site; env must be realized there.",
    "kernel_restart": "Start a NEW kernel replaying the old transcript's "
                      "successful blocks; returns the new kernel_id.",
    "kernel_stop": "Stop a kernel (transcript remains readable).",
}


def build_tool_defs(weft_cls) -> list[dict]:
    from .api import PUBLIC_TOOLS
    defs = []
    for name in PUBLIC_TOOLS:
        fn = inspect.unwrap(getattr(weft_cls, name))
        sig = inspect.signature(fn)
        hints = typing.get_type_hints(fn) if fn.__annotations__ else {}
        props, required = {}, []
        for pname, param in sig.parameters.items():
            if pname == "self":
                continue
            schema = _json_type(hints.get(pname, param.annotation))
            if param.default is inspect.Parameter.empty:
                required.append(pname)
            else:
                schema = {**schema, "default": param.default} \
                    if param.default is not None else schema
            hint = SCHEMA_HINTS.get(name, {}).get(pname)
            if hint:
                schema = {**schema, **hint}
            props[pname] = schema
        defs.append({
            "name": name,
            "description": inspect.getdoc(fn)
            or TOOL_DESCRIPTIONS.get(name, name),
            "inputSchema": {"type": "object", "properties": props,
                            "required": required},
        })
    return defs


def serve(weft, stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    tool_defs = build_tool_defs(type(weft))

    def reply(msg_id, result=None, error=None) -> None:
        out = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            out["error"] = error
        else:
            out["result"] = result
        stdout.write(json.dumps(out) + "\n")
        stdout.flush()

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, msg_id = msg.get("method"), msg.get("id")
        if method == "initialize":
            reply(msg_id, {
                "protocolVersion": msg.get("params", {}).get(
                    "protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "weft", "version": "0.1.0"},
            })
        elif method == "notifications/initialized":
            continue  # notification: no reply
        elif method == "tools/list":
            reply(msg_id, {"tools": tool_defs})
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name")
            args = params.get("arguments") or {}
            if name not in {t["name"] for t in tool_defs}:
                reply(msg_id, error={"code": -32602,
                                     "message": f"unknown tool {name!r}"})
                continue
            try:
                result = getattr(weft, name)(**args)
            except TypeError as e:  # bad arguments — schema violation
                reply(msg_id, error={"code": -32602, "message": str(e)})
                continue
            payload = json.dumps(result, default=str)
            reply(msg_id, {
                "content": [{"type": "text", "text": payload}],
                "isError": isinstance(result, dict) and "error" in result,
            })
        elif msg_id is not None:
            reply(msg_id, error={"code": -32601,
                                 "message": f"method {method!r} not supported"})


def main() -> None:
    import argparse
    from .api import Weft
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--pixi-bin", default=None)
    args = ap.parse_args()
    serve(Weft(args.workspace, pixi_bin=args.pixi_bin))


if __name__ == "__main__":
    main()
