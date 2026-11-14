"""MCP server tests — scripted stdio, stubbed checkers, no network.

The server is driven in-process over StringIO: requests go in as
JSON lines, responses come back as JSON lines. The registry
checks run against the real sample registry; the deep/dev checks
run against stub factories so no live RPC call is ever made.
"""
import io
import json

from src.mcp_server import McpServer

FLAGGED = "SAMPLEDEV11111111111111111111111111111111"
UNKNOWN = "Unknown111111111111111111111111111111"


class _StubReport:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


class _StubChecker:
    def __init__(self, data):
        self._data = data

    def check(self, *args, **kwargs):
        return _StubReport(self._data)


class _FailingChecker:
    def check(self, *args, **kwargs):
        raise ValueError("boom")


def drive(raw_lines, **kwargs):
    """Feed raw input lines to a server; return parsed response dicts."""
    text = "".join(line if line.endswith("\n") else line + "\n" for line in raw_lines)
    stdin = io.StringIO(text)
    stdout = io.StringIO()
    server = McpServer(stdin=stdin, stdout=stdout, **kwargs)
    assert server.run() == 0
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def req(msg_id, method, params=None):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


def test_initialize_handshake():
    (resp,) = drive([req(1, "initialize", {"protocolVersion": "2024-11-05"})])
    assert resp["id"] == 1
    result = resp["result"]
    assert result["serverInfo"]["name"] == "rug-farm-registry"
    assert result["serverInfo"]["version"] == "2.18"
    assert "tools" in result["capabilities"]


def test_initialize_echoes_client_version():
    (resp,) = drive([req(1, "initialize", {"protocolVersion": "2025-03-26"})])
    assert resp["result"]["protocolVersion"] == "2025-03-26"


def test_initialized_notification_gets_no_reply():
    responses = drive(
        [
            req(1, "initialize", {}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            req(2, "ping"),
        ]
    )
    assert [r["id"] for r in responses] == [1, 2]


def test_ping():
    (resp,) = drive([req(7, "ping")])
    assert resp["result"] == {}


def test_tools_list_returns_four_tools():
    (resp,) = drive([req(1, "tools/list")])
    tools = resp["result"]["tools"]
    assert [t["name"] for t in tools] == [
        "rug_check",
        "deep_check",
        "dev_check",
        "registry_status",
    ]
    for tool in tools:
        assert "description" in tool and "inputSchema" in tool
        assert ">= 2 rugged mints" in tool["description"]
        assert "no data is ever invented" in tool["description"]


def test_rug_check_flagged_and_unknown():
    responses = drive(
        [
            req(1, "tools/call", {"name": "rug_check", "arguments": {"dev_wallet": FLAGGED}}),
            req(2, "tools/call", {"name": "rug_check", "arguments": {"dev_wallet": UNKNOWN}}),
        ]
    )
    flagged = json.loads(responses[0]["result"]["content"][0]["text"])
    assert flagged["flagged"] is True
    assert flagged["risk_level"] == "high"
    assert responses[0]["result"]["isError"] is False
    unknown = json.loads(responses[1]["result"]["content"][0]["text"])
    assert unknown["flagged"] is False
    assert unknown["risk_level"] == "unknown"


def test_rug_check_missing_wallet_is_error():
    (resp,) = drive([req(1, "tools/call", {"name": "rug_check", "arguments": {}})])
    assert resp["result"]["isError"] is True
    assert "dev_wallet" in resp["result"]["content"][0]["text"]


def test_deep_check_uses_stub_factory():
    stub = {"mint": "MINT111", "verdict": "clean", "flagged": False}
    (resp,) = drive(
        [req(1, "tools/call", {"name": "deep_check", "arguments": {"mint": "MINT111"}})],
        deep_checker_factory=lambda registry: _StubChecker(stub),
    )
    assert resp["result"]["isError"] is False
    assert json.loads(resp["result"]["content"][0]["text"]) == stub


def test_dev_check_uses_stub_factory():
    stub = {"dev_wallet": "WALLET111", "verdict": "under_review"}
    (resp,) = drive(
        [req(1, "tools/call", {"name": "dev_check", "arguments": {"wallet": "WALLET111"}})],
        dev_checker_factory=lambda: _StubChecker(stub),
    )
    assert resp["result"]["isError"] is False
    assert json.loads(resp["result"]["content"][0]["text"]) == stub


def test_registry_status():
    (resp,) = drive([req(1, "tools/call", {"name": "registry_status", "arguments": {}})])
    status = json.loads(resp["result"]["content"][0]["text"])
    assert status["status"] == "ok"
    assert status["entries"] == 4
    assert status["flagged"] == 2
    assert status["sample_data"] is True


def test_unknown_tool_is_error_result():
    (resp,) = drive([req(1, "tools/call", {"name": "nope", "arguments": {}})])
    assert resp["result"]["isError"] is True
    assert "Unknown tool" in resp["result"]["content"][0]["text"]


def test_unknown_method_is_jsonrpc_error():
    (resp,) = drive([req(1, "resources/list")])
    assert resp["error"]["code"] == -32601
    assert "resources/list" in resp["error"]["message"]


def test_malformed_json_is_parse_error_and_server_keeps_running():
    responses = drive(["not json at all", req(2, "ping")])
    assert responses[0]["error"]["code"] == -32700
    assert responses[0]["id"] is None
    assert responses[1]["id"] == 2
    assert responses[1]["result"] == {}


def test_tool_error_is_error_result_not_crash():
    (resp,) = drive(
        [req(1, "tools/call", {"name": "deep_check", "arguments": {"mint": "MINT111"}})],
        deep_checker_factory=lambda registry: _FailingChecker(),
    )
    assert resp["result"]["isError"] is True
    assert "boom" in resp["result"]["content"][0]["text"]


def test_eof_exits_cleanly_with_no_output():
    assert drive([]) == []


def test_batch_request():
    responses = drive(
        [
            json.dumps(
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                ]
            )
        ]
    )
    assert [r["id"] for r in responses] == [1, 2]
    assert len(responses[1]["result"]["tools"]) == 4
