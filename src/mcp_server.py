"""MCP server for the Rug-Farm Registry (v2.18) — stdio JSON-RPC 2.0.

Ported from SolanaOS (github.com/x402agent/solanaos,
``mcp-server``): SolanaOS exposes its tool registry over MCP
(``tools/list`` + ``tools/call``, resources, prompts) via the
MCP SDK. This server implements the same ``tools/list`` +
``tools/call`` surface for the registry in stdlib only (``json``,
``sys``) — no MCP SDK, no new dependencies — over
newline-delimited JSON-RPC on stdin/stdout, so any MCP client
(Claude Desktop, Cursor, VS Code) can call the registry's
checks as tools.

Tools call the existing check modules — no logic is
reimplemented here. Every tool description states the verdict
rule (flagged needs >= 2 rugged mints + evidence; a suspicious
pattern alone is never flagged) and that no data is ever
invented.

Run:  python -m src.mcp_server   (reads stdin until EOF)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .deep_check import DeepChecker
from .dev_check import DevChecker, HeliusSource, PublicRpcSource
from .live_scan import HeliusClient
from .registry import check_wallet, list_flagged, load_registry

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "data" / "registry.json"

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "rug-farm-registry"
SERVER_VERSION = "2.18"

VERDICT_RULE = (
    "Verdict rule: a dev is flagged only when >= 2 rugged mints "
    "with evidence are found; a suspicious pattern alone is never "
    "flagged, and no data is ever invented."
)

TOOLS: list[dict] = [
    {
        "name": "rug_check",
        "description": (
            "Check a dev wallet against the Rug-Farm Registry, "
            "optionally for a specific mint. Returns the risk level, "
            "reasons and evidence links. " + VERDICT_RULE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dev_wallet": {
                    "type": "string",
                    "description": "Dev wallet address to check",
                },
                "mint": {
                    "type": "string",
                    "description": "Mint address (optional)",
                },
            },
            "required": ["dev_wallet"],
        },
    },
    {
        "name": "deep_check",
        "description": (
            "Run the full deep check for a mint: live coin data, "
            "creator/dev history, side wallets, clusters and wash "
            "score, in one report. " + VERDICT_RULE + " Note: a "
            "full keyless deep check can take minutes on a throttled "
            "public RPC."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mint": {
                    "type": "string",
                    "description": "Mint address to deep-check",
                },
            },
            "required": ["mint"],
        },
    },
    {
        "name": "dev_check",
        "description": (
            "Dev-wallet-first check: profile, funder chain two hops "
            "up, and the bundled-wallet test for a dev wallet. "
            + VERDICT_RULE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet": {
                    "type": "string",
                    "description": "Dev wallet address to check",
                },
            },
            "required": ["wallet"],
        },
    },
    {
        "name": "registry_status",
        "description": (
            "Health summary of the registry: version, entry count "
            "and flagged count. " + VERDICT_RULE
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


def _default_registry_path() -> Path:
    return Path(os.environ.get("REGISTRY_PATH", DEFAULT_REGISTRY_PATH))


def _default_registry_loader() -> dict:
    return load_registry(_default_registry_path())


def _default_deep_checker(registry: dict) -> DeepChecker:
    return DeepChecker(registry_data=registry)


def _default_dev_checker() -> DevChecker:
    """DevChecker over the live chain source (same as dev_check.main)."""
    helius = HeliusClient()
    if helius.enabled:
        source = HeliusSource(client=helius)
    else:
        source = PublicRpcSource()
        if not source.probe():
            raise RuntimeError("public Solana RPC unreachable")
    return DevChecker(source)


def _text_result(text: str, *, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def _json_result(data) -> dict:
    return _text_result(json.dumps(data, indent=2, default=str))


class McpServer:
    """Newline-delimited JSON-RPC 2.0 MCP server over stdio.

    ``registry_loader`` / ``deep_checker_factory`` /
    ``dev_checker_factory`` are injectable so tests can drive the
    server with stubs and no network: the registry loader returns
    a registry dict; the deep-checker factory takes a registry
    dict and returns an object with ``check(mint)``; the
    dev-checker factory takes no arguments and returns an object
    with ``check(wallet)``. Both checkers' ``check`` results must
    offer ``to_dict()``.
    """

    def __init__(
        self,
        *,
        registry_loader=None,
        deep_checker_factory=None,
        dev_checker_factory=None,
        stdin=None,
        stdout=None,
    ) -> None:
        self._registry_loader = registry_loader or _default_registry_loader
        self._deep_checker_factory = (
            deep_checker_factory or _default_deep_checker
        )
        self._dev_checker_factory = (
            dev_checker_factory or _default_dev_checker
        )
        self._in = stdin if stdin is not None else sys.stdin
        self._out = stdout if stdout is not None else sys.stdout

    # -- transport --------------------------------------------------

    def run(self) -> int:
        """Read JSON-RPC requests until EOF; 0 on a clean exit."""
        for line in self._in:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self._write(
                    self._error_response(None, PARSE_ERROR, "Parse error")
                )
                continue
            response = self.handle(message)
            if response is None:
                continue
            if isinstance(response, list):
                for item in response:
                    self._write(item)
            else:
                self._write(response)
        return 0

    def _write(self, response: dict) -> None:
        self._out.write(json.dumps(response) + "\n")
        self._out.flush()

    # -- dispatch ---------------------------------------------------

    def handle(self, message) -> dict | list | None:
        """Handle one parsed JSON-RPC message.

        Returns a response dict, a list of them (batch), or None
        for a notification (no reply is ever sent for one).
        """
        if isinstance(message, list):
            responses = []
            for item in message:
                response = self.handle(item)
                if response is not None:
                    responses.append(response)
            return responses or None
        if not isinstance(message, dict):
            return self._error_response(None, INVALID_REQUEST, "Invalid Request")
        method = message.get("method")
        if not isinstance(method, str):
            return self._error_response(
                message.get("id"), INVALID_REQUEST, "Invalid Request"
            )
        has_id = "id" in message
        msg_id = message.get("id")
        if method.startswith("notifications/"):
            return None  # Notifications never get a reply.
        if not has_id:
            return None  # A request without an id is a notification.
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return self._error_response(
                msg_id, INVALID_PARAMS, "params must be an object"
            )
        handler = {
            "initialize": self._on_initialize,
            "ping": self._on_ping,
            "tools/list": self._on_tools_list,
            "tools/call": self._on_tools_call,
        }.get(method)
        if handler is None:
            return self._error_response(
                msg_id, METHOD_NOT_FOUND, f"Method not found: {method}"
            )
        try:
            result = handler(params)
        except Exception as exc:  # A handler must never crash the server.
            return self._error_response(msg_id, -32603, f"Internal error: {exc}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error_response(msg_id, code: int, message: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": message},
        }

    # -- methods ----------------------------------------------------

    def _on_initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        return {
            "protocolVersion": requested
            if isinstance(requested, str) and requested
            else PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    @staticmethod
    def _on_ping(params: dict) -> dict:
        return {}

    @staticmethod
    def _on_tools_list(params: dict) -> dict:
        return {"tools": TOOLS}

    def _on_tools_call(self, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str):
            return _text_result("Error: tools/call requires a tool name", is_error=True)
        if not isinstance(args, dict):
            return _text_result(
                "Error: tools/call arguments must be an object", is_error=True
            )
        try:
            if name == "rug_check":
                return self._tool_rug_check(args)
            if name == "deep_check":
                return self._tool_deep_check(args)
            if name == "dev_check":
                return self._tool_dev_check(args)
            if name == "registry_status":
                return self._tool_registry_status(args)
        except Exception as exc:
            return _text_result(f"Error: {exc}", is_error=True)
        return _text_result(f"Error: Unknown tool: {name}", is_error=True)

    # -- tools ------------------------------------------------------

    def _tool_rug_check(self, args: dict) -> dict:
        dev_wallet = args.get("dev_wallet")
        if not isinstance(dev_wallet, str) or not dev_wallet:
            return _text_result(
                "Error: Missing required argument: dev_wallet", is_error=True
            )
        mint = args.get("mint")
        registry = self._registry_loader()
        result = check_wallet(registry, dev_wallet, mint=mint)
        return _json_result(result)

    def _tool_deep_check(self, args: dict) -> dict:
        mint = args.get("mint")
        if not isinstance(mint, str) or not mint:
            return _text_result(
                "Error: Missing required argument: mint", is_error=True
            )
        registry = self._registry_loader()
        checker = self._deep_checker_factory(registry)
        report = checker.check(mint)
        return _json_result(report.to_dict())

    def _tool_dev_check(self, args: dict) -> dict:
        wallet = args.get("wallet")
        if not isinstance(wallet, str) or not wallet:
            return _text_result(
                "Error: Missing required argument: wallet", is_error=True
            )
        checker = self._dev_checker_factory()
        report = checker.check(wallet)
        return _json_result(report.to_dict())

    def _tool_registry_status(self, args: dict) -> dict:
        registry = self._registry_loader()
        entries = registry.get("entries", [])
        return _json_result(
            {
                "status": "ok",
                "version": registry.get("version"),
                "sample_data": bool(registry.get("sample_data")),
                "entries": len(entries),
                "flagged": len(list_flagged(registry)),
            }
        )


def main(argv: list[str] | None = None) -> int:
    """Entry point: ``python -m src.mcp_server``."""
    return McpServer().run()


if __name__ == "__main__":
    raise SystemExit(main())
