"""MCP transport: schemas from one source of truth, uniform error payloads,
and a real job driven end-to-end over stdio JSON-RPC."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[2] / "src")


class Client:
    def __init__(self, workspace: str, pixi_bin: str):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "weft.mcp_server",
             "--workspace", workspace, "--pixi-bin", pixi_bin],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            env={"PYTHONPATH": SRC, "PATH": "/usr/bin:/bin"},
        )
        self._id = 0

    def rpc(self, method: str, params: dict | None = None):
        self._id += 1
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": method,
             "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        return json.loads(line)

    def call(self, tool: str, **args):
        r = self.rpc("tools/call", {"name": tool, "arguments": args})
        assert "result" in r, r
        payload = json.loads(r["result"]["content"][0]["text"])
        return payload, r["result"]["isError"]

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


@pytest.fixture
def client(tmp_path, pixi_bin):
    c = Client(str(tmp_path / "ws"), pixi_bin)
    init = c.rpc("initialize", {"protocolVersion": "2024-11-05"})
    assert init["result"]["serverInfo"]["name"] == "weft"
    yield c
    c.close()


def test_tools_list_schemas(client):
    from weft.api import PUBLIC_TOOLS
    tools = client.rpc("tools/list")["result"]["tools"]
    assert {t["name"] for t in tools} == set(PUBLIC_TOOLS)
    submit = next(t for t in tools if t["name"] == "task_submit")
    assert submit["inputSchema"]["required"] == ["task"]
    assert "plan" in submit["description"] or "submit" in submit["description"].lower()
    kexec = next(t for t in tools if t["name"] == "kernel_exec")
    assert "persistent state" in kexec["description"]


def test_full_job_over_mcp(client, tmp_path):
    payload, is_err = client.call(
        "register_site", name="local", kind="local",
        config={"root": str(tmp_path / "site")})
    assert not is_err and payload["site"] == "local"

    payload, is_err = client.call("task_submit", task={
        "command": "echo mcp-ok > results/o.txt",
        "outputs": ["results/"], "site": "local"})
    assert not is_err, payload
    job_id = payload["job_id"]
    for _ in range(100):
        st, _ = client.call("task_status", job_id=job_id)
        if st[0]["state"] in ("DONE", "FAILED"):
            break
        time.sleep(0.3)
    result, is_err = client.call("task_result", job_id=job_id)
    assert not is_err
    out = next(o for o in result["outputs"] if o["path"] == "results/o.txt")
    assert out["preview"]["lines"] == ["mcp-ok"]
    prov, _ = client.call("provenance", target=job_id)
    assert prov["command"].startswith("echo mcp-ok")


def test_errors_are_flagged_payloads(client):
    payload, is_err = client.call("task_result", job_id="jb_nonexistent")
    assert is_err and payload["error"] == "task.invalid"
    # bad arguments → JSON-RPC error, not a crash
    r = client.rpc("tools/call", {"name": "task_result", "arguments":
                                  {"nope": 1}})
    assert r["error"]["code"] == -32602
    # unknown tool
    r2 = client.rpc("tools/call", {"name": "frobnicate", "arguments": {}})
    assert r2["error"]["code"] == -32602
