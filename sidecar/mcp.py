"""Model Context Protocol (MCP) stdio server over a Context Fabric store.

Spec target: MCP ``2025-11-25`` — the latest revision of the handshake era
(``initialize`` / ``notifications/initialized``); the ``2026-07-28`` revision removed
``initialize`` entirely, so this server speaks the newest revision that still has it.

Wire format: newline-delimited JSON-RPC 2.0 on stdio. stdout carries protocol
messages only; all logging goes to stderr. Supported subset: ``initialize``, ``ping``,
``tools/list``, ``tools/call``; every notification is accepted and ignored; resources,
prompts, and logging are not implemented.

``memory_search`` scoring is deliberately local and deterministic (query-token
occurrence counts), not the separate BM25 effort; it is replaceable behind the same
tool signature.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from kernel.store import SqliteStore, StoreConflict
from protocol.canonical import sha256_hex
from protocol.epoch import Epoch, verify_chain
from protocol.models import LifecycleStatus
from sidecar import obsfold
from sidecar.blocks import build_block, stored_block

SERVER_NAME = "context-fabric"
SERVER_VERSION = "0.1.0"
LATEST_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2024-11-05")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
READONLY_ERROR = -32001  # implementation-defined server error

_MAX_EXCERPT = 300
_MAX_SEARCH_RESULTS = 50

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "memory_search",
        "description": (
            "Search Context Fabric memory blocks by deterministic lexical match over "
            "their contents. Returns a JSON object with a ranked `results` list; each "
            "result has block_id, block_kind, content_excerpt (at most 300 chars), "
            "lifecycle_status, and score. Quarantined draft blocks are excluded unless "
            "include_quarantined is true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Words to match against block contents.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 5,
                    "description": "Maximum results to return (capped at 50).",
                },
                "include_quarantined": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include draft (quarantined) blocks. Default false.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "receipt_verify",
        "description": (
            "Verify a signed epoch receipt chain (JSON array of epoch objects, e.g. the "
            "contents of a receipts epochs file). Returns a JSON object with `ok` and, "
            "on failure, `reason`. The out-of-band Ed25519 public key is read from the "
            "server's CF_RECEIPT_PUBKEY environment variable (hex)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "epochs_json": {
                    "type": "string",
                    "description": "JSON array of signed epoch receipt objects.",
                },
            },
            "required": ["epochs_json"],
        },
    },
    {
        "name": "memory_add",
        "description": (
            "Append one memory block to the fabric. Blocks are quarantined as draft "
            "unless the server was started with --trusted; draft blocks are denied by the "
            "policy gate until promoted. Disabled when the server runs with --readonly. "
            "Returns block_id and lifecycle_status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "block_kind": {
                    "type": "string",
                    "description": "Block kind (decision, fact, observation, ...).",
                },
                "content": {"type": "string", "description": "Non-empty block content."},
            },
            "required": ["block_kind", "content"],
        },
    },
    {
        "name": "expand",
        "description": (
            "Expand a folded observation stub (ctx://obs/<id>, or a bare obs_<id> id) back "
            "to the full stored content. Looks up the observation block in this server's "
            "store; returns `found: false` when it is not present."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "The ctx://obs/<id> stub URI to expand.",
                },
            },
            "required": ["uri"],
        },
    },
]


class JsonRpcError(Exception):
    """A JSON-RPC error object to return for the in-flight request."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": is_error,
    }


def _str_arg(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise JsonRpcError(INVALID_PARAMS, f"{name} must be a non-empty string")
    return value


def _bool_arg(arguments: dict[str, Any], name: str, default: bool) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise JsonRpcError(INVALID_PARAMS, f"{name} must be a boolean")
    return value


def _int_arg(arguments: dict[str, Any], name: str, default: int) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise JsonRpcError(INVALID_PARAMS, f"{name} must be an integer >= 1")
    return value


def _excerpt(text: str) -> str:
    return text if len(text) <= _MAX_EXCERPT else text[: _MAX_EXCERPT - 3] + "..."


def _score(query_tokens: list[str], text: str) -> int:
    counts = Counter(_TOKEN_RE.findall(text.lower()))
    return sum(counts[token] for token in query_tokens)


class FabricServer:
    """JSON-RPC request handler over one SqliteStore (single-threaded)."""

    def __init__(self, store: SqliteStore, *, readonly: bool, trusted: bool = False) -> None:
        self._store = store
        self._readonly = readonly
        # Trust is a server-side setting, never a tool argument (audit AU2-H2).
        self._trusted = trusted

    def handle(self, raw: str) -> dict[str, Any] | None:
        """Handle one raw JSON line; ``None`` means no response (notification)."""
        try:
            message = json.loads(raw)
        except ValueError:
            return _error(None, PARSE_ERROR, "Parse error")
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "Invalid Request")
        method = message.get("method")
        if not isinstance(method, str):
            # A response to a server-initiated request (we send none) — ignore it.
            return None if "id" in message else _error(None, INVALID_REQUEST, "Invalid Request")
        if "id" not in message:
            # Notification (initialized, cancelled, unknown): never answered.
            return None
        request_id = message["id"]
        params = message.get("params")
        if params is None:
            params = {}
        elif not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "params must be an object")
        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": _TOOLS}
            elif method == "tools/call":
                result = self._tools_call(params)
            else:
                return _error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        except JsonRpcError as exc:
            return _error(request_id, exc.code, exc.message)
        except (sqlite3.Error, OSError, ValidationError) as exc:
            return _error(request_id, INTERNAL_ERROR, f"store error: {exc}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            raise JsonRpcError(INVALID_PARAMS, "protocolVersion is required")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise JsonRpcError(INVALID_PARAMS, "tools/call requires a tool name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        elif not isinstance(arguments, dict):
            raise JsonRpcError(INVALID_PARAMS, "arguments must be an object")
        if name == "memory_search":
            return _tool_result(self._memory_search(arguments))
        if name == "receipt_verify":
            return _tool_result(self._receipt_verify(arguments))
        if name == "memory_add":
            return _tool_result(self._memory_add(arguments))
        if name == "expand":
            return _tool_result(self._expand(arguments))
        raise JsonRpcError(INVALID_PARAMS, f"Unknown tool: {name}")

    def _memory_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = _str_arg(arguments, "query")
        limit = min(_int_arg(arguments, "limit", 5), _MAX_SEARCH_RESULTS)
        include_quarantined = _bool_arg(arguments, "include_quarantined", False)
        tokens = _TOKEN_RE.findall(query.lower())
        scored: list[tuple[int, dict[str, Any]]] = []
        for block in self._store.load_blocks():
            if not include_quarantined and block.lifecycle_status is LifecycleStatus.draft:
                continue
            text = block.content or block.ref_uri or ""
            score = _score(tokens, text)
            if score > 0:
                scored.append(
                    (
                        score,
                        {
                            "block_id": block.block_id,
                            "block_kind": block.block_kind.value,
                            "content_excerpt": _excerpt(text),
                            "lifecycle_status": block.lifecycle_status.value,
                            "score": score,
                        },
                    )
                )
        scored.sort(key=lambda pair: -pair[0])
        return {"query": query, "results": [item for _, item in scored[:limit]]}

    def _receipt_verify(self, arguments: dict[str, Any]) -> dict[str, Any]:
        epochs_json = _str_arg(arguments, "epochs_json")
        try:
            raw = json.loads(epochs_json)
        except ValueError as exc:
            raise JsonRpcError(INVALID_PARAMS, f"epochs_json is not valid JSON: {exc}") from exc
        if not isinstance(raw, list) or not raw:
            raise JsonRpcError(INVALID_PARAMS, "epochs_json must be a non-empty JSON array")
        try:
            epochs = [Epoch.model_validate(item) for item in raw]
        except ValidationError as exc:
            raise JsonRpcError(INVALID_PARAMS, f"invalid epoch record: {exc}") from exc
        pubkey_hex = os.environ.get("CF_RECEIPT_PUBKEY", "")
        if not pubkey_hex:
            return {"ok": False, "reason": "CF_RECEIPT_PUBKEY is not set; cannot verify"}
        try:
            public_key = bytes.fromhex(pubkey_hex)
        except ValueError:
            return {"ok": False, "reason": "CF_RECEIPT_PUBKEY is not valid hex"}
        expected_key_id = sha256_hex(public_key)[:16]
        if any(epoch.key_id != expected_key_id for epoch in epochs):
            return {"ok": False, "reason": "key_id mismatch between epoch and provided key"}
        try:
            verified = verify_chain(epochs, public_key)
        except (ValueError, UnicodeError) as exc:
            # e.g. a lone surrogate in a string field cannot be canonicalized to UTF-8
            raise JsonRpcError(INVALID_PARAMS, f"invalid epoch record: {exc}") from exc
        if not verified:
            return {
                "ok": False,
                "reason": "epoch chain verification failed (tamper, reorder, or bad signature)",
            }
        return {"ok": True, "epochs_verified": len(epochs), "key_id": expected_key_id}

    def _memory_add(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._readonly:
            raise JsonRpcError(READONLY_ERROR, "readonly mode: memory_add is disabled")
        block_kind = _str_arg(arguments, "block_kind")
        content = _str_arg(arguments, "content")
        try:
            block = build_block(
                {"block_kind": block_kind, "content": content},
                trusted=self._trusted,
                event_seq=self._store.max_source_event_seq() + 1,
            )
            inserted = self._store.append(block)
        except ValueError as exc:
            raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
        except StoreConflict as exc:
            raise JsonRpcError(INTERNAL_ERROR, f"store conflict: {exc}") from exc
        stored = block if inserted else (stored_block(self._store, block.block_id) or block)
        return {
            "block_id": stored.block_id,
            "content_hash": stored.content_hash,
            "lifecycle_status": stored.lifecycle_status.value,
            "source_event_seq": stored.source_event_seq,
            "deduplicated": not inserted,
        }

    def _expand(self, arguments: dict[str, Any]) -> dict[str, Any]:
        uri = _str_arg(arguments, "uri")
        content = obsfold.expand(self._store, uri)
        if content is None:
            return {"uri": uri, "found": False}
        return {"uri": uri, "found": True, "content": content}


def serve(store: SqliteStore, *, readonly: bool, trusted: bool = False) -> int:
    """Run the stdio loop until EOF; one JSON-RPC message per line."""
    server = FabricServer(store, readonly=readonly, trusted=trusted)
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            response = server.handle(line)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
    except BrokenPipeError:
        return 1
    return 0


def _configure_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            # errors="replace" keeps a malformed UTF-8 line from killing the stdio loop;
            # the replacement characters then fail JSON parsing -> -32700, as intended.
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cfx mcp", description="MCP stdio server over a Context Fabric SQLite store."
    )
    parser.add_argument(
        "--store",
        default=os.environ.get("CF_FABRIC") or None,
        help="SQLite store path (.db/.sqlite) (env: CF_FABRIC)",
    )
    parser.add_argument(
        "--readonly",
        action="store_true",
        help="reject writes: memory_add returns a JSON-RPC error (env: CF_FABRIC)",
    )
    parser.add_argument(
        "--trusted",
        action="store_true",
        help="memory_add writes routable instead of quarantined draft (server-side trust)",
    )
    args = parser.parse_args(argv)
    if not args.store:
        print("cfx mcp: --store (or CF_FABRIC) is required", file=sys.stderr)
        return 2
    path = Path(args.store)
    if args.readonly and not path.is_file():
        print(
            f"cfx mcp: store not found: {path} (readonly mode will not create it)", file=sys.stderr
        )
        return 2
    try:
        store = SqliteStore(path)
    except (sqlite3.Error, OSError) as exc:
        print(f"cfx mcp: cannot open store {path}: {exc}", file=sys.stderr)
        return 2
    _configure_stdio()
    print(
        f"context-fabric mcp: store={path} readonly={args.readonly}",
        file=sys.stderr,
    )
    return serve(store, readonly=args.readonly, trusted=args.trusted)


if __name__ == "__main__":
    raise SystemExit(main())
