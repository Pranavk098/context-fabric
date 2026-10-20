"""Verify that every required/forbidden string in b4v3_objectives.json exists in the httpx source.

Also checks that recorded source excerpts match the actual file lines and that the
fixture meets the file-coverage / cross-file constraints.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HTPX_ROOT = (
    Path(os.environ.get("CF_B4_FIXTURE", Path(tempfile.gettempdir()) / "opencode" / "httpx_pilot"))
    / "httpx"
)
JSON_PATH = Path(__file__).with_name("b4v3_objectives.json")


def main() -> int:
    objectives = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    errors: list[str] = []
    files_covered: set[str] = set()
    cross_file_count = 0

    for obj in objectives:
        obj_id = obj["id"]
        source_files = {s["file"] for s in obj["source"]}
        files_covered.update(source_files)
        if len(source_files) >= 2:
            cross_file_count += 1

        # Build per-file line maps for files referenced by this objective.
        file_lines: dict[str, dict[int, str]] = {}
        file_texts: dict[str, str] = {}
        for src in obj["source"]:
            fname = src["file"]
            if fname not in file_texts:
                text = (HTPX_ROOT / fname).read_text(encoding="utf-8")
                file_texts[fname] = text
                file_lines[fname] = {
                    i: ln.rstrip() for i, ln in enumerate(text.splitlines(), start=1)
                }

        # Verify each recorded excerpt matches the actual line.
        for src in obj["source"]:
            actual = file_lines[src["file"]].get(src["line"], "")
            if actual.rstrip() != src["excerpt"].rstrip():
                errors.append(f"{obj_id}: excerpt mismatch at {src['file']}:{src['line']}")

        # Required strings must appear in at least one referenced source file.
        for req in obj["required"]:
            found = any(req in file_texts[f] for f in source_files)
            if not found:
                errors.append(f"{obj_id}: required string {req!r} not found in referenced files")

        # Forbidden strings must also exist in the referenced source files.
        for bad in obj["forbidden"]:
            found = any(bad in file_texts[f] for f in source_files)
            if not found:
                errors.append(f"{obj_id}: forbidden string {bad!r} not found in referenced files")

    if len(files_covered) < 12:
        errors.append(f"Only {len(files_covered)} distinct files covered (need >= 12)")
    if cross_file_count < 8:
        errors.append(f"Only {cross_file_count} cross-file objectives (need >= 8)")

    print(f"Objectives checked: {len(objectives)}")
    print(f"Distinct httpx files covered: {len(files_covered)}")
    print(f"Cross-file objectives: {cross_file_count}")
    if errors:
        print("FAILURES:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
