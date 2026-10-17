# Research Audit — Citation Verification (2026-10-17)

Independent anti-hallucination audit of the four research documents produced on 2026-10-17:
`PLUG-PRIOR-ART.md`, `PROVENANCE-PRIOR-ART.md`, `SHARED-MEMORY-PRIOR-ART.md`, `PATH-VIABILITY.md`.
A separate auditor agent fetched every cited arXiv paper and checked product, funding, and
timeline claims against primary sources. This file records what was checked, what was
corrected, and what remains unverified.

## Method

- Every arXiv ID in the four documents (~40) was fetched at `arxiv.org/abs/<id>` and checked
  for existence and whether its content matches the document's characterization.
- Product/funding/timeline claims were checked against primary sources (vendor docs, GitHub
  repos, funding announcements, regulator pages).
- Vendor self-published performance numbers (Zep benchmarks, Mooncake "75% more requests",
  AAFLOW+ analytical figures) remain labeled as vendor claims — not re-benchmarked here.
- Corrections were applied in place. Nothing was removed for being fabricated — no fabricated
  citation was found.

## Corrections applied (5)

| # | Document(s) | Change | Basis |
| :--- | :--- | :--- | :--- |
| 1 | `SHARED-MEMORY-PRIOR-ART.md`, `SOTA-AND-THREATS.md`, `SHARED-COPY.md` | 2609.04748 trajectory-divergence numbers updated from 36.2%/75.0% to **37.4% (16-bit) / 82.5% (4-bit)** | paper v2 (rev. 2026-10-12) |
| 2 | `PLUG-PRIOR-ART.md` | 2602.11988 (AGENTS.md study) updated to v3 numbers: developer files **+2.4%** (n.s.), LLM-generated **−0.5%/−2%** (n.s.), cost **+20%/+23%** | paper v3 |
| 3 | `PLUG-PRIOR-ART.md` | CLIProxyAPI stars 52.6k → **55.5k** | GitHub |
| 4 | `PLUG-PRIOR-ART.md` | AutoGen status: maintenance mode → **fully archived** (merged into Microsoft Agent Framework) | repo + migration guide |
| 5 | `SHARED-MEMORY-PRIOR-ART.md` | `[UNVERIFIED]` markers removed for **2512.12818** (Hindsight) and **2605.14421** (MemLineage) — both confirmed to exist and match | arXiv |

## Verified highlights (sampled from ~70 checked claims)

- **arXiv IDs:** all ~40 cited papers exist and match; key ones exact: 2609.01931 (Agent Flight
  Recorder, ~48 µs/event, tamper detection), 2605.11032 (Merkle-DAG + BLAKE3 + Ed25519),
  2607.02574 ("rarely state a consistency contract"), 2610.05833 (69.0% vs 26.1% answer
  variation, ~5.7× TTFT), 2604.06370 (ForkKV +3.0×), 2510.12872 (KVCOMM >70% reuse / 7.8×),
  2605.14498 (GroupMemBench ~46%), 2503.03704 (MINJA 98.2%), 2409.05014 (SLSA study),
  2506.09501 (BF16 ~9% accuracy std-dev), 2505.22778 (HF GPG <2%).
- **Products/funding:** vLLM Semantic Router 6.2k★; LiteLLM 60k+★ / 511M+ pulls; Mem0 $24M
  (Oct 2025) + mem0-mcp archived; Zep CE discontinued (Apr 2025) + Graphiti 20k★; Portkey
  $15M Series A + PANW acquisition; Helicone→Mintlify; MCP registry 41.4k servers (Oct 2026);
  Claude Marketplace 2,000+ plugins; Ollama $65M / ~9M users; OpenRouter $113M Series B;
  W&B–CoreWeave ≈$1.7B reported; Langfuse–ClickHouse (stays MIT).
- **Regulatory/timeline:** EU AI Act Article 50 (Aug 2026) plus high-risk deadlines (Dec 2027 /
  Aug 2028) per the Omnibus delays; Gartner $492M→$1B+; SR 26-2 (Apr 2026); ISO 42001 ~220 certs.
- **Provenance tech:** Thinking Machines batch-invariance (Sep 2025); SGLang deterministic mode
  (~34% overhead); OpenSSF model-signing 1.0 (Apr 2025); Sigstore graduated; C2PA 6,000+ members.

## Remaining [UNVERIFIED] (deliberate hedges; none load-bearing)

- Adept/Amazon deal details and Steamship's final status (`PATH-VIABILITY.md`) — press-reported.
- Channel-mix causality for dev-tool launches (`PLUG-PRIOR-ART.md` §2 honest gap).
- IETF AFiR draft-profile claim and small-operator compliance demand (`PROVENANCE-PRIOR-ART.md`).
- Vendor self-published numbers stay labeled as vendor claims.

## Confidence summary

- Checked: ~40 arXiv IDs + ~30 web/product/funding claims.
- Corrected: 5 (two were superseded-revision numbers). Fabrications found: **0**. Removed: 0.
- The four documents' conclusions **stand**: the plug shape (OpenAI-compatible sidecar), the
  seal's minimum claim scope ("seals the context, not the model's answer"), the "contract +
  lineage + replay" positioning for the shared copy, and the path-viability warnings are all
  backed by verified sources. The corrected numbers do not change any conclusion.
