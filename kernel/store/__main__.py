"""CLI: ``python -m kernel.store ingest <file.jsonl> <db>`` and ``info <db>``."""

from __future__ import annotations

import argparse

from kernel.store.sqlite_store import SqliteStore


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m kernel.store",
        description="Context Fabric durable block store (SQLite v0).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="ingest a JSONL fabric file into a store")
    ingest.add_argument("jsonl", help="JSONL file with one ContextBlock per line")
    ingest.add_argument("db", help="SQLite database path (created if missing)")
    info = commands.add_parser("info", help="print block count and max source_event_seq")
    info.add_argument("db", help="SQLite database path")
    args = parser.parse_args()
    if args.command == "ingest":
        store = SqliteStore(args.db)
        inserted, deduplicated = store.ingest_jsonl(args.jsonl)
        print(f"{args.db}: {inserted} inserted, {deduplicated} deduplicated")
    elif args.command == "info":
        store = SqliteStore(args.db)
        print(
            f"{args.db}: blocks={store.count()}, "
            f"max_source_event_seq={store.max_source_event_seq()}"
        )


if __name__ == "__main__":
    main()
