"""`.cfx` cartridge serializer — the Tier 0 Project Hologram.

Ranks files by PageRank centrality, renders the top-ranked module signatures into a
deterministic markdown hologram within a token budget, and computes a stable prefix hash
for KV-cache pinning. Zero LLM tokens.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import tiktoken

from kernel.cartridge.ast_extractor import FileSymbols
from kernel.cartridge.pagerank_map import build_graph, pagerank
from protocol.canonical import sha256_hex

_ENCODER = tiktoken.get_encoding("o200k_base")


def count_tokens(text: str) -> int:
    return len(_ENCODER.encode(text))


@dataclass(frozen=True)
class Cartridge:
    cartridge_id: str
    tenant_id: str
    project_id: str
    commit_hash: str
    hologram_markdown: str
    token_count: int
    kv_prefix_hash: str


def build_cartridge(
    files: Sequence[FileSymbols],
    *,
    tenant_id: str,
    project_id: str,
    commit_hash: str,
    budget_tokens: int = 600,
) -> Cartridge:
    ranks = pagerank(build_graph(files))
    ranked = sorted(files, key=lambda f: (-ranks.get(f.path, 0.0), f.path))

    lines = [f"# Project Hologram: {project_id} @ {commit_hash[:8]}"]
    for f in ranked:
        sig = "; ".join(s.signature for s in f.exported) or "(no exports)"
        candidate = f"- {f.path}: {sig}"
        if count_tokens("\n".join([*lines, candidate])) > budget_tokens:
            break
        lines.append(candidate)

    markdown = "\n".join(lines)
    return Cartridge(
        cartridge_id=f"cfx_{project_id}_{commit_hash[:8]}",
        tenant_id=tenant_id,
        project_id=project_id,
        commit_hash=commit_hash,
        hologram_markdown=markdown,
        token_count=count_tokens(markdown),
        kv_prefix_hash="sha256:" + sha256_hex(markdown.encode("utf-8")),
    )


if __name__ == "__main__":
    from kernel.cartridge.ast_extractor import extract_source

    f1 = extract_source("core.py", "def used_by_all():\n    ...\n")
    f2 = extract_source(
        "app.py", "from core import used_by_all\n\ndef main():\n    used_by_all()\n"
    )
    c = build_cartridge([f1, f2], tenant_id="t", project_id="p", commit_hash="deadbeef00")
    assert c.token_count > 0 and c.hologram_markdown.startswith("# Project Hologram")
    assert c.cartridge_id == "cfx_p_deadbeef"
    assert c.kv_prefix_hash.startswith("sha256:")
    # determinism
    assert build_cartridge([f2, f1], tenant_id="t", project_id="p", commit_hash="deadbeef00") == c
    print("serializer.py self-check: OK")
