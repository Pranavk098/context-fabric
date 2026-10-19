# SKIM — question-guided hand-off skimmer (B8b)

**Status: EXPLORATORY. Nothing in this file is a claim of record.** The offline table below
is a recall diagnostic on the B4 v3 dev set (`evals/fixtures/b4v3_objectives.json`, frozen
sha256). A later, separately pre-registered experiment on held-out objectives would be
required to claim any accuracy win.

## What it is

`kernel/router/skim.py` is a deterministic, no-LLM hand-off source: given a task/objective
string and a set of source files, it produces a compact packet of **real** source lines —
verbatim, each anchored with `===== FILE: <path>:<line> =====` — that fits a token budget,
in three steps:

1. **coarse** — chunk each file the same way the frozen B4 v3 retrieval arm does
   (`evals.b4_v3.build_ast_chunks`): one module-header chunk (docstring + import statements)
   and one chunk per top-level function/class (full body via `ast.get_source_segment`). Rank
   chunks by Okapi BM25 against the objective (shared `kernel.router.recall.tokenize`).
2. **fine** — inside each kept chunk, split the body into contiguous non-blank line blocks
   and keep the top BM25-ranked blocks, always keeping the def/class signature line and the
   import lines that bind names used in the kept lines.
3. **pack** — greedily under the token budget, preserving real source lines verbatim.

No RNG, no wall clock, no unordered iteration (ADR-0003). Token budget is measured with the
caller's counter (default `count_tokens`, tiktoken `o200k_base`).

## Compiler flag

`skim` is exposed as an optional hand-off source on the compiler path behind a keyword
flag, **default OFF**:

* `kernel.router.compiler.compile_packet(..., handoff_source=None)` — when `None` (the
  default), packets are byte-identical to before this change. When a skimmed string is
  passed, the packet carries the instruction plus that verbatim source as its single Tier-3
  observation (asserted in `tests/test_skim.py::test_handoff_flag_off_is_byte_identical`).
* `adapters.langgraph.compile_node_packet(..., handoff_source=None)` forwards it 1:1.

The sidecar and `cfx fork` call `compile_packet` without the flag, so they are unaffected.

## Offline eval (exploratory recall diagnostic)

`evals/b8_skim_offline.py` (`--dry-run`, no GPU, no API) compares three hand-off packets at
1k/2k/4k token budgets over the full `httpx` package (23 files, ~64k tokens, the B4 v3
dev-set objectives). Tokenizer is `tiktoken` `o200k_base` (the Qwen tokenizer is not
available in this offline environment). Recall is the fixed-scorer token match
(leading/trailing `.`/`-`/`/` stripped — the B4 v3 correction).

| arm | budget | prompt tokens (20 reqs) | max per req | obj. w/ all facts | fact coverage |
|---:|---:|---:|---:|---:|---:|
| R (BM25 chunks) | 1k | 20,470 | 1,036 | 9 / 20 | 19 / 33 |
| **SKIM** | 1k | 20,273 | 1,024 | 7 / 20 | 18 / 33 |
| C (old compile packet) | 1k | 21,962 | 1,139 | 12 / 20 | 22 / 33 |
| R (BM25 chunks) | 2k | 40,494 | 2,039 | 15 / 20 | 27 / 33 |
| **SKIM** | 2k | 40,134 | 2,027 | 12 / 20 | 23 / 33 |
| C (old compile packet) | 2k | 34,498 | 1,765 | 15 / 20 | 26 / 33 |
| R (BM25 chunks) | 4k | 80,495 | 4,040 | 16 / 20 | 28 / 33 |
| **SKIM** | 4k | 79,782 | 4,008 | 12 / 20 | 23 / 33 |
| C (old compile packet) | 4k | 56,021 | 2,913 | 16 / 20 | 28 / 33 |

**Reading (exploratory only).** SKIM is the cheapest arm at every budget but, on this dev
set, recalls *fewer* required facts than plain BM25 chunks at equal budget (18–23 vs 19–28
facts). The gap comes from the coarse-then-fine split: SKIM trims the top-ranked chunks
(the same ones R keeps whole) and spends the freed budget on lower-ranked chunks whose lines
carry fewer required facts. The old compile packet matches R on fact coverage at 2k/4k and
leads at 1k, while being cheaper than R at 2k/4k. These are all single-hop identifier/value
recall figures on a small dev set — **not** a task-success or quality claim, and not a
statement about what a larger model would do.
