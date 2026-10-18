"""``cfx`` — one command for the Context Fabric sidecar: serve, add, verify."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from kernel.store import SqliteStore, StoreConflict
from protocol import epoch
from sidecar import demo, mcp
from sidecar.blocks import build_block, stored_block
from sidecar.replay import replay, seal
from sidecar.server import serve


def _add_serve_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--upstream",
        default=os.environ.get("CF_UPSTREAM", "http://127.0.0.1:8000"),
        help="OpenAI-compatible upstream base URL (env: CF_UPSTREAM)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("CF_PORT", "8080")),
        help="local port to listen on (env: CF_PORT)",
    )
    parser.add_argument(
        "--fabric",
        default=os.environ.get("CF_FABRIC") or None,
        help="JSONL file or SQLite (.db/.sqlite) store of ContextBlock records (env: CF_FABRIC)",
    )
    parser.add_argument(
        "--mode",
        choices=("compile", "passthrough"),
        default=os.environ.get("CF_MODE", "compile"),
        help="compile injects packets; passthrough is the honest A/B arm (env: CF_MODE)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cfx",
        description="Context Fabric sidecar: OpenAI-compatible proxy, writable memory, receipts.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="run the OpenAI-compatible sidecar proxy")
    _add_serve_arguments(serve_parser)

    def _serve(args: argparse.Namespace) -> int:
        if args.mode not in ("compile", "passthrough"):
            serve_parser.error(
                f"invalid mode {args.mode!r} (set --mode or CF_MODE to compile or passthrough)"
            )
        try:
            serve(
                upstream=args.upstream,
                port=args.port,
                fabric=Path(args.fabric) if args.fabric else None,
                mode=args.mode,
            )
        except FileNotFoundError as exc:
            print(f"cfx serve: {exc}", file=sys.stderr)
            return 2
        return 0

    serve_parser.set_defaults(func=_serve)

    add_parser = subparsers.add_parser("add", help="append one block JSON to a SQLite fabric")
    add_parser.add_argument("--store", required=True, help="SQLite store path (.db/.sqlite)")
    add_parser.add_argument("--file", required=True, help="block JSON file, or - for stdin")
    add_parser.add_argument(
        "--trusted",
        action="store_true",
        help="write as routable instead of quarantined draft (default: draft)",
    )
    add_parser.set_defaults(func=_cmd_add)

    verify_parser = subparsers.add_parser("verify", help="verify a signed epoch receipt chain")
    verify_parser.add_argument("epochs", help="JSON file with a list of epochs")
    verify_parser.add_argument("--pubkey", default=None, help="Ed25519 public key hex")
    verify_parser.set_defaults(func=_cmd_verify)

    replay_parser = subparsers.add_parser(
        "replay", help="recompile a logged packet from its manifest and compare hashes"
    )
    replay_parser.add_argument("--store", required=True, help="SQLite store path (.db/.sqlite)")
    replay_parser.add_argument(
        "--packet-id", required=True, help="packet id (the X-Fabric-Packet-Id header value)"
    )
    replay_parser.set_defaults(func=_cmd_replay)

    seal_parser = subparsers.add_parser(
        "seal", help="commit the compile log as a signed epoch receipt chain"
    )
    seal_parser.add_argument("--store", required=True, help="SQLite store path (.db/.sqlite)")
    seal_parser.add_argument("--private-key", required=True, help="Ed25519 32-byte seed hex")
    seal_parser.add_argument(
        "--out", required=True, help="epochs JSON file (appended if it already exists)"
    )
    seal_parser.add_argument(
        "--model", default=os.environ.get("CF_MODEL", ""), help="model id metadata (env: CF_MODEL)"
    )
    seal_parser.add_argument(
        "--engine",
        default=os.environ.get("CF_ENGINE", ""),
        help="engine metadata (env: CF_ENGINE)",
    )
    seal_parser.set_defaults(func=_cmd_seal)

    mcp_parser = subparsers.add_parser("mcp", help="run the stdio MCP server for agent clients")
    mcp_parser.add_argument(
        "--store",
        default=os.environ.get("CF_FABRIC") or None,
        help="SQLite store path (.db/.sqlite) (env: CF_FABRIC)",
    )
    mcp_parser.add_argument(
        "--readonly", action="store_true", help="reject writes: memory_add returns an error"
    )
    mcp_parser.set_defaults(func=_cmd_mcp)

    demo_parser = subparsers.add_parser(
        "demo", help="run the fleet/negative demo against a live upstream and write a receipt"
    )
    demo.add_arguments(demo_parser)
    demo_parser.set_defaults(func=demo.main_from_namespace)

    return parser


def _cmd_add(args: argparse.Namespace) -> int:
    try:
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
        payload: Any = json.loads(text)
    except (OSError, ValueError) as exc:
        print(f"cfx add: cannot read block JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("cfx add: block JSON must be an object", file=sys.stderr)
        return 2
    try:
        store = SqliteStore(args.store)
    except (sqlite3.Error, OSError) as exc:
        print(f"cfx add: cannot open store {args.store}: {exc}", file=sys.stderr)
        return 2
    try:
        block = build_block(
            payload, trusted=args.trusted, event_seq=store.max_source_event_seq() + 1
        )
    except ValueError as exc:
        print(f"cfx add: invalid block: {exc}", file=sys.stderr)
        return 2
    try:
        inserted = store.append(block)
    except StoreConflict as exc:
        print(f"cfx add: {exc}", file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError) as exc:
        print(f"cfx add: store error: {exc}", file=sys.stderr)
        return 1
    if not inserted:
        block = stored_block(store, block.block_id) or block
    print(f"block_id: {block.block_id}")
    print(f"content_hash: {block.content_hash}")
    print(f"lifecycle_status: {block.lifecycle_status.value}")
    if not inserted:
        print("already present")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    argv = [args.epochs]
    if args.pubkey:
        argv += ["--pubkey", args.pubkey]
    return epoch.main(["verify", *argv])


def _cmd_replay(args: argparse.Namespace) -> int:
    return replay(args.store, args.packet_id)


def _cmd_seal(args: argparse.Namespace) -> int:
    return seal(args.store, args.private_key, args.out, model=args.model, engine=args.engine)


def _cmd_mcp(args: argparse.Namespace) -> int:
    argv = ["--store", args.store] if args.store else []
    if args.readonly:
        argv.append("--readonly")
    return mcp.main(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point; bare flags/empty argv keep ``python -m sidecar --port N`` working."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] in (["-h"], ["--help"]):
        _build_parser().print_help()
        return 0
    if not args or args[0].startswith("-"):
        args = ["serve", *args]
    parsed = _build_parser().parse_args(args)
    result: Any = parsed.func(parsed)
    return int(result) if result is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
