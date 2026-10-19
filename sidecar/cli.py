"""``cfx`` — one command for the Context Fabric sidecar: serve, add, verify."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from kernel.mmu.epoch_folder import DEFAULT_K
from kernel.store import SqliteStore, StoreConflict
from protocol import epoch
from sidecar import b8, demo, foldfork, mcp, obsfold
from sidecar.blocks import build_block, stored_block
from sidecar.replay import replay, seal
from sidecar.repro import run_repro
from sidecar.server import open_store, serve


def _int_env(name: str, default: int | None = None) -> int | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


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
    parser.add_argument(
        "--obs-fold",
        action="store_true",
        default=os.environ.get("CF_OBS_FOLD", "") in ("1", "true", "yes", "on"),
        help="fold older tool observations into reversible ctx://obs stubs (env: CF_OBS_FOLD)",
    )
    parser.add_argument(
        "--obs-store",
        default=os.environ.get("CF_OBS_STORE") or None,
        help="SQLite store for folded observation content + fold receipt (env: CF_OBS_STORE)",
    )
    parser.add_argument(
        "--obs-n",
        type=int,
        default=obsfold.DEFAULT_N,
        help=f"keep last N observations verbatim (default {obsfold.DEFAULT_N})",
    )
    parser.add_argument(
        "--obs-k",
        type=int,
        default=obsfold.DEFAULT_K,
        help=f"fold in chunks of K observations (default {obsfold.DEFAULT_K})",
    )
    parser.add_argument(
        "--obs-max-tokens",
        type=int,
        default=None,
        help="window guard: fold more, oldest first, until the request fits (optional)",
    )
    parser.add_argument(
        "--window-cap",
        type=int,
        default=_int_env(b8.WINDOW_CAP_ENV),
        help=(
            "hard sub-agent input window cap: 400 context_length_exceeded when "
            "input+max_tokens exceeds N (env: CF_B8_WINDOW_CAP)"
        ),
    )
    parser.add_argument(
        "--b8-ledger-dir",
        default=os.environ.get(b8.LEDGER_DIR_ENV) or None,
        help=(
            "enable B8 money safety (model allow-list + daily token ledger) writing JSONL "
            "under this directory (env: CF_B8_LEDGER_DIR)"
        ),
    )
    parser.add_argument(
        "--b8-daily-cap",
        type=int,
        default=_int_env(b8.DAILY_CAP_ENV, b8.DEFAULT_DAILY_CAP),
        help=f"daily token cap for the B8 ledger (default {b8.DEFAULT_DAILY_CAP}) "
        "(env: CF_B8_DAILY_CAP)",
    )
    parser.add_argument(
        "--kv-hints",
        action="store_true",
        default=os.environ.get("CF_KV_HINTS", "") in ("1", "true", "yes", "on"),
        help=(
            "attach a vLLM KvHintsEnvelope (PR #53423) to upstream requests in compile "
            "mode; off by default, passthrough stays byte-identical (env: CF_KV_HINTS)"
        ),
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
        obs_store: SqliteStore | None = None
        if args.obs_fold and args.obs_store:
            try:
                obs_store = open_store(args.obs_store)
            except (sqlite3.Error, OSError) as exc:
                print(f"cfx serve: cannot open obs store {args.obs_store}: {exc}", file=sys.stderr)
                return 2
        b8_ledger: b8.Ledger | None = None
        if args.b8_ledger_dir:
            b8_ledger = b8.Ledger(args.b8_ledger_dir)
        try:
            serve(
                upstream=args.upstream,
                port=args.port,
                fabric=Path(args.fabric) if args.fabric else None,
                mode=args.mode,
                obs_fold=args.obs_fold,
                obs_store=obs_store,
                obs_n=args.obs_n,
                obs_k=args.obs_k,
                obs_max_tokens=args.obs_max_tokens,
                window_cap=args.window_cap,
                b8_ledger=b8_ledger,
                b8_daily_cap=args.b8_daily_cap,
                kv_hints=args.kv_hints,
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

    fold_parser = subparsers.add_parser(
        "fold", help="fold raw tool-output events into Tier-1 facts (append-only, idempotent)"
    )
    fold_parser.add_argument("--store", required=True, help="SQLite store path (.db/.sqlite)")
    fold_parser.add_argument(
        "--k",
        type=int,
        default=DEFAULT_K,
        help=f"micro-epoch size in raw events (default {DEFAULT_K})",
    )
    fold_parser.set_defaults(func=_cmd_fold)

    fork_parser = subparsers.add_parser(
        "fork", help="spawn a COW child packet from the parent receipt logged for --packet-id"
    )
    fork_parser.add_argument("--store", required=True, help="SQLite store path (.db/.sqlite)")
    fork_parser.add_argument(
        "--packet-id", required=True, help="parent packet id (the X-Fabric-Packet-Id value)"
    )
    fork_parser.add_argument("--agent", required=True, help="child agent id")
    fork_parser.add_argument("--role", required=True, help="child role (RBAC-filtered)")
    fork_parser.add_argument("--objective", required=True, help="child objective")
    fork_parser.add_argument(
        "--stage", default=None, help="child stage (default: the parent manifest's stage)"
    )
    fork_parser.add_argument("--out", default=None, help="also write the child packet JSON here")
    fork_parser.set_defaults(func=_cmd_fork)

    expand_parser = subparsers.add_parser(
        "expand", help="expand a folded ctx://obs/<id> stub back to its stored content"
    )
    expand_parser.add_argument("--store", required=True, help="SQLite store path (.db/.sqlite)")
    expand_parser.add_argument("uri", help="the ctx://obs/<id> stub (or bare obs_<id> id)")
    expand_parser.set_defaults(func=_cmd_expand)

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

    repro_parser = subparsers.add_parser(
        "repro", help="recompute the offline-verifiable numbers from their artifacts"
    )
    repro_parser.add_argument(
        "--verbose", "-v", action="store_true", help="print every offline check, not just failures"
    )
    repro_parser.set_defaults(func=_cmd_repro)

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


def _cmd_fold(args: argparse.Namespace) -> int:
    return foldfork.fold(args.store, k=args.k)


def _cmd_fork(args: argparse.Namespace) -> int:
    return foldfork.fork(
        args.store,
        args.packet_id,
        agent=args.agent,
        role=args.role,
        objective=args.objective,
        stage=args.stage,
        out_path=args.out,
    )


def _cmd_expand(args: argparse.Namespace) -> int:
    """Print the full content behind a ``ctx://obs/<id>`` stub; exit 1 when not stored."""
    try:
        store = SqliteStore(args.store)
    except (sqlite3.Error, OSError) as exc:
        print(f"cfx expand: cannot open store {args.store}: {exc}", file=sys.stderr)
        return 2
    content = obsfold.expand(store, args.uri)
    if content is None:
        print(f"cfx expand: {args.uri} not found in {args.store}", file=sys.stderr)
        return 1
    sys.stdout.write(content)
    if not content.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _cmd_seal(args: argparse.Namespace) -> int:
    return seal(args.store, args.private_key, args.out, model=args.model, engine=args.engine)


def _cmd_repro(args: argparse.Namespace) -> int:
    return run_repro(verbose=args.verbose)


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
