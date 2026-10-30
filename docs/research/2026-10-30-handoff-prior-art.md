# Sub-agent handoff: prior art (2026-10-30)

Time-boxed (about 15 tool calls). Every row marked **[fetched]** was read via WebFetch this session; **[search]** means only a search-result snippet was seen (not the page); **[memory]** means unverified recollection, do not cite without checking. Search snippets are model-summarised and may be wrong.

What we built (from `docs/plans/2026-10-30-brief-first-plan.md`, `docs/decisions/ADR-0011-brief-v1.md`): a PreToolUse hook on `Agent` that appends to the parent's delegation prompt a per-child-model budgeted packet (Haiku 6 KB, Sonnet 12 KB, Opus 20 KB). It holds relevance-ranked file pointers, hash-verified line-range excerpts of what the parent read or edited, and rule-extracted goal/constraints/decisions/tried taken from user and parent text but never tool output. The pointer view is emitted as packet v0. Evidence so far: none (Gate-2 replay and the v4 delegation benchmark are pending).

## Table

| System | What it does for child context | Evidence it works | Link |
|---|---|---|---|
| Claude Code sub-agents (non-fork) **[fetched]** | Fresh window. Child gets its own system prompt, a parent-written delegation message, the CLAUDE.md hierarchy, a git-status snapshot, listed skills, sibling roster. No conversation, no files already read, no auto memory. Docs tell users to restate must-follow rules in the delegation prompt. No automatic relevant-context attachment. | None given; design statement only | https://code.claude.com/docs/en/sub-agents |
| Claude Code fork **[fetched, partial]** | Fork inherits the full parent conversation (over-briefing end of the spectrum). Fork details were in the unread tail of the page. | None given | same |
| Anthropic multi-agent research system **[fetched]** | Lead agent writes each sub-agent an objective, output format, tool guidance and boundaries; plan saved to external memory; sub-agents write artifacts to external store and return references to avoid "telephone". | Whole system: +90.2% vs single Opus 4 on an internal research eval; vague briefs caused duplicated work (anecdote). No ablation of brief content. | https://www.anthropic.com/engineering/multi-agent-research-system |
| Cognition "Don't build multi-agents" **[fetched]** | Argues for sharing full traces; proposes an LLM that compresses history into key details/events/decisions (they say they fine-tuned a small one). | Explicitly illustrative; no benchmarks | https://cognition.com/blog/dont-build-multi-agents |
| OpenAI Agents SDK handoffs **[fetched]** | Receiver sees full history by default; `input_filter` lets the developer trim; opt-in beta `nest_handoff_history` compacts history into summary segments. Developer-authored filters, no relevance ranking, no verification. | None given | https://openai.github.io/openai-agents-python/handoffs/ |
| Codex CLI sub-agents **[search]** | Fork semantics: child inherits parent context window, sandbox, overrides; `fork_turns` default "all" in MultiAgentV2 (per a community blog/issue). Community tools offer fully isolated alternatives. | None seen | https://codex.danielvaughan.com/2026/05/20/detached-subagent-patterns-codex-cli-context-free-orchestration-community-tools/ , https://github.com/openai/codex/issues/16475 |
| Microsoft Copilot Studio, Mastra **[search]** | Pass full parent history by default with a switch/`messageFilter` to trim. | None seen | https://learn.microsoft.com/en-us/microsoft-copilot-studio/guidance/generative-orchestration-subagents , https://mastra.ai/docs/subagents |
| polyc-agent `handoff.rs` **[search]** | `max_carry` knob for how much parent context to carry. Small crate, unclear maturity. | None seen | https://docs.rs/crate/polyc-agent/latest/source/src/handoff.rs |
| Handoff Debt (KC, Budathoki), arXiv 2606.02875 **[fetched abstract]** | Benchmark of interrupted-task takeover; successors get repo-only vs raw trace vs summary notes vs structured notes. | 724 takeover runs per model, 3 models: context-bearing handoffs cut median events 20-59% and cumulative prompt tokens 42-63% vs repo-only; solved-rate effect smaller and model-dependent | https://arxiv.org/abs/2606.02875 |
| DeLM, arXiv 2606.10662 **[fetched abstract]** | Replaces orchestrator with shared context plus task queue so agents see peers' findings and do not redo work. Different architecture (shared blackboard, not parent-to-child brief). | Up to +17.5 pts accuracy, up to 2.49x speed vs Codex, Claude Code and native sub-agents on Terminal-Bench, DeepSWE, SWE-bench Verified (maxima only) | https://arxiv.org/abs/2606.10662 |
| ACON, arXiv 2510.00615 (ICML 2026) **[search]** | Learns compression instructions from failures to shrink agent context; distils to small compressors. Compresses a single agent's history, not a parent-to-child brief. | 26-54% peak-token cut, performance largely kept; distilled compressors keep over 95% accuracy (snippet) | https://arxiv.org/pdf/2510.00615 |
| Cemri et al., arXiv 2503.13657 **[search, via agentpatterns.ai]** | Failure taxonomy of 1,600+ multi-agent traces; inter-agent misalignment is one category. | Taxonomy only; no brief method | https://agentpatterns.ai/patterns/multi-agent/subagent-inheritance-contract/ |
| Aider repo map / RepoMapper MCP **[search]** | Graph-ranked repo map under a token budget, with mentioned identifiers weighted 10x and chat files 50x. Single-agent context, not a handoff; closest prior art to our ranking idea. | None seen | https://glama.ai/mcp/servers/pdavis68/RepoMapper |
| Iterative-retrieval skill pattern **[search]** | Child runs 3 retrieval cycles scoring files 0-1, keeps at least 0.7. Child-side discovery, not parent-side handoff. | None seen | https://tessl.io/registry/jbvc/iterative-retrieval |
| Community PreToolUse-on-Agent hook **[search]** | A blog describes a hook matching the Agent tool that injects `additionalContext` into the sub-agent prompt. Same trigger mechanism as ours; snippet says details partial and undocumented. | None | https://www.bluehost.com/blog/?p=276907 (unverified) |
| LangGraph handoffs / Command, MemGPT/Letta, Cursor, Cline, Devin, OpenHands **[memory, not fetched]** | Believed: LangGraph passes shared graph state; MemGPT tiers memory; Cursor/Cline/OpenHands sub-agents mostly isolated or full-state. Not checked. | Unknown | not fetched |
| OpenCode **[not found]** | Search returned nothing on its task tool. | Unknown | none |

## What is genuinely not covered (in what I saw)

1. **No product I found automatically builds a task-relevant, budgeted brief from the parent's observed working set.** Docs for Claude Code say the parent must write everything into the delegation message. Others default to full history (Codex fork, OpenAI SDK, Mastra, Copilot Studio) with a manual trim knob.
2. **Model-aware sizing of the packet for the child** (Haiku vs Sonnet budgets): not seen anywhere.
3. **Verified excerpts** (content hashed against disk at brief time, degrading to pointer if stale): not seen. Compressors (Cognition, ACON, SDK nest-history) are LLM summaries with no verification.
4. **A trust rule that keeps tool output out of the child prompt** (injection-aware intent sourcing): not seen; SDK docs explicitly say nested history does not redact.
5. **A benchmark that measures brief quality directly** (planted facts that exist nowhere in the repo; child re-reads of briefed files). Closest is Handoff Debt, but it measures interrupted-task takeover with trace/notes, not parent-to-child delegation or brief selection, and it shows efficiency gains are more reliable than solve-rate gains. That is a caution for our primary outcome.
6. Receipts that join what the child received to what it re-read anyway.

Caveat: absence in about 15 calls is weak evidence. I did not search Cursor, Cline, Devin or OpenHands docs, nor GitHub/npm for Claude Code plugins.

## Packet-design ideas worth stealing if Gate-2 replay fails

- **Handoff Debt's four views** (repo-only / raw trace / summary notes / structured notes): reuse as ablation arms for our packet, so a null shows which component carries value. Structured notes plus efficiency metrics (events, prompt tokens) rather than solve rate.
- **Artifact references instead of excerpts** (Anthropic): put content in a file the child can open and pass only a pointer; avoids the byte cap and the injection surface. We already have `obs/` folds.
- **Aider-style weighting**: identifiers mentioned in the delegation prompt weighted heavily (10x) plus a binary-search fit to the token budget in rank order; replaces our lexicographic tiers with a score and exact budget fill.
- **Objective / output format / tool guidance / boundaries template** (Anthropic): a fixed four-field brief skeleton for the intent section instead of regex-extracted sentences, if extraction proves noisy.
- **Cognition-style trained compressor** or **ACON failure-driven instruction tuning**: replace rule-based intent extraction with a learned one, tuned from child failures (planted-fact misses). Costly, and ADR-0011 currently forbids LLM summaries.
- **Shared blackboard (DeLM)**: if parent-to-child briefs show no gain, a shared findings store written by children and read by siblings may be the better abstraction; our carry-back is a weak form of it.
- **Child-side iterative retrieval** as the fallback: if a good parent brief cannot be predicted, let the child pull context via a cheap `cfxd` query tool instead of being pushed it.
- **Fork as baseline** is already in v4; Codex defaulting to full-history fork makes it the realistic competitor, not an over-briefing strawman.

## Risk of being scooped / already solved: verdict

**Scooped on the general problem: low-moderate (about 25%), confidence medium-low.** The idea is obvious, and the field (Handoff Debt, DeLM, Cognition, Anthropic write-up) is converging on it. Handoff Debt (arXiv 2606.02875) shows context-bearing handoffs cut tokens 42-63%, which both validates the premise and means "handoffs help" is not novel.

**Already solved by a shipped product: low (about 10%), confidence low-medium.** Nothing I read ships automatic relevance-ranked, model-sized, verified briefs. The main risk is platform absorption: Claude Code or Codex adding automatic context carry (Codex already forks full history; Claude Code has forks on by default) would make a plugin moot, and a platform-native fork removes most of the over/under-briefing problem for cost-insensitive users. Our defensible niche is cost: a small brief to a small model versus a fork.

**Novelty is in the combination and the measurement, not any single idea** (ranking is Aider's; summaries are Cognition's; structured notes are Handoff Debt's). A null result on `brief_v1` vs `none` remains publishable only if the planted-fact design holds up, since that measurement is the part I found no prior art for.

Unchecked and could change the verdict: Cursor/Cline/Devin/OpenHands/LangGraph/Letta internals, GitHub plugins doing the PreToolUse-on-Agent trick, papers after search coverage, and the Claude Code fork section (unread tail of the docs page).
