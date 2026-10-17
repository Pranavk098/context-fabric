"""CLI entry point: ``python -m sidecar [--upstream URL] [--port N] [--fabric PATH] [--mode M]``."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from sidecar.server import serve


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m sidecar",
        description="OpenAI-compatible Context Fabric sidecar proxy.",
    )
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
    args = parser.parse_args()
    if args.mode not in ("compile", "passthrough"):
        parser.error(
            f"invalid mode {args.mode!r} (set --mode or CF_MODE to compile or passthrough)"
        )
    serve(
        upstream=args.upstream,
        port=args.port,
        fabric=Path(args.fabric) if args.fabric else None,
        mode=args.mode,
    )


if __name__ == "__main__":
    main()
