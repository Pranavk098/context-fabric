# Small Code Skimmers for Hand-Offs (B3 / B8b Follow-Up)

- **Task:** W2 — Research small trained and training-free code skimmers for hand-offs (B3)
- **Date:** 2026-10-19
- **Status:** Research & Architecture Proposal (docs only; no runtime code modified)
- **Target Hardware:** Local NVIDIA RTX 5070 (8 GB VRAM, WSL2; `Qwen/Qwen2.5-0.5B-Instruct` and `Qwen/Qwen2.5-1.5B-Instruct` cached locally)
- **Baseline Reference:** `docs/engineering/SKIM.md`, `kernel/router/skim.py`, `evals/b8_skim_offline.py`, `evals/fixtures/b4v3_objectives.json`

---

## 0. Root-Cause Diagnosis: Why Keyword `SKIM` Lost to Plain BM25 (`R`)

On the 20-objective B4 v3 dev set (`evals/fixtures/b4v3_objectives.json`, 33 required facts across 23 `httpx` files / 64,066 `tiktoken` tokens), `evals/b8_skim_offline.py` measured (`docs/engineering/SKIM.md`):

| Budget | `R` (whole AST chunks, BM25) | `SKIM` (`kernel/router/skim.py`) | Delta (`SKIM - R`) |
|---:|---:|---:|---:|
| 1,024 | 19/33 (57.6%) | 18/33 (54.5%) | -1 (-3.0 pp) |
| 2,048 | 27/33 (81.8%) | 23/33 (69.7%) | -4 (-12.1 pp) |
| 4,096 | 28/33 (84.8%) | 23/33 (69.7%) | -5 (-15.2 pp) |

Inspecting `kernel/router/skim.py` alongside `evals/b4_v3.py` (`build_ast_chunks` and `_retrieval_messages`) identifies three structural failure modes in `kernel/router/skim.py`:

1. **Zero-BM25 Intra-Function Fact Dropping (`_symbol_blocks`, lines 231–263):**
   After ranking coarse AST chunks by BM25, `skim()` splits each symbol's body on blank lines (`_split_blocks`) and scores each contiguous paragraph independently with lexical BM25 against `objective`:
   ```python
   scores = bm25_scores(objective, blocks)
   order = sorted(range(len(body)), key=lambda i: (-scores[i], body[i].start_line))
   positive = [i for i in order if scores[i] > 0.0]
   chosen = positive if positive else order[:1]
   ```
   In real functions and classes, the objective's lexical terms frequently match the function signature or docstring (`sig` block), while the required fact (a return literal, status code, default constant, or raised exception) sits in a body paragraph that shares **zero lexical tokens** with `objective` (`scores[i] == 0.0`). `SKIM` drops those `scores[i] == 0.0` blocks out of the #1-ranked chunk (which `R` keeps whole) and spends the freed token budget on lower-ranked chunks (`chunk #5, #6, ...`) that happen to share a common query token.
2. **Per-Block Header Tax (`_render`, lines 214–215):**
   `R` emits a single `===== FILE: <rel> =====` header per AST chunk. `SKIM` wraps the signature *and every surviving non-blank paragraph* in its own `===== FILE: <rel>:<start_line> =====` header (~9–12 tokens each), inflating metadata overhead on multi-paragraph classes and functions.
3. **Out-of-Order Intra-Function Emission (`_symbol_blocks`, line 255):**
   Surviving blocks inside a symbol are emitted in descending BM25 order (`for idx in chosen`) rather than source line order (`start_line`), breaking control-flow and data-flow continuity for downstream reader models.

Both **SWE-Pruner** (contextual cross-attention + CRF sequence labeling over the full file/chunk) and **LongCodeZip** (causal conditional-perplexity / Approximated Mutual Information + preserved structural blocks + 0/1 knapsack in source order) directly address these failure modes because contextual representations propagate query relevance from a matched signature or variable definition to the downstream lines that compute or return it.

---

## 1. Exact Selection Mechanics: SWE-Pruner vs. LongCodeZip

### 1.1 SWE-Pruner (arXiv:2601.16746)

- **Primary Sources Opened (all accessed 2026-10-19):**
  - Paper abstract & HTML: <https://arxiv.org/abs/2601.16746>, <https://arxiv.org/html/2601.16746v1>
  - Repository (`public` branch): <https://github.com/Ayanami1314/swe-pruner>, <https://raw.githubusercontent.com/Ayanami1314/swe-pruner/public/README.md>
  - Inference & architecture code:
    - <https://raw.githubusercontent.com/Ayanami1314/swe-pruner/public/swe-pruner/src/swe_pruner/configuration.py>
    - <https://raw.githubusercontent.com/Ayanami1314/swe-pruner/public/swe-pruner/src/swe_pruner/model_structure.py>
    - <https://raw.githubusercontent.com/Ayanami1314/swe-pruner/public/swe-pruner/src/swe_pruner/prune_wrapper.py>
    - <https://raw.githubusercontent.com/Ayanami1314/swe-pruner/public/swe-pruner/src/swe_pruner/swepruner.py>
    - <https://raw.githubusercontent.com/Ayanami1314/swe-pruner/public/swe-pruner/src/swe_pruner/online_serving.py>
  - Training documentation: <https://raw.githubusercontent.com/Ayanami1314/swe-pruner/public/train/README.md>
  - Weights & config: <https://huggingface.co/ayanami-kitasan/code-pruner/raw/main/config.json>, <https://huggingface.co/ayanami-kitasan/code-pruner/raw/main/README.md>

#### (a) Inputs & Prompt Formatting
- **Inputs:**
  1. `query` ($q$): A natural-language goal hint formulated by the agent before reading/skimming (exposed to the agent as `context_focus_question`, e.g., *"Focus on the MRO resolution logic and class inheritance hierarchy"*).
  2. `code` ($C$): Raw source file or multi-file command output.
- **Prompt Template (`prune_wrapper.py` lines 29–35, 149–154):**
  Uses the exact `Qwen3-Reranker` chat wrapper:
  ```text
  <|im_start|>system
  Judge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>
  <|im_start|>user
  <Instruct>: Given a query, judge if the document(code) is related to query.
  <Query>: {query}
  <Document>: {code}<|im_end|>
  <|im_start|>assistant
  <think>

  </think>

  ```
- **Context Length & Chunking (`prune_wrapper.py` lines 35, 315–465):**
  - Maximum sequence length: `max_length = 8192` tokens (`left` padding, `return_offsets_mapping=True`).
  - If `prefix + query + code + suffix` exceeds 8,192 tokens, `split_code_into_chunks()` slices `code` into overlapping token windows of size `available_length = 8192 - non_code_len - 10` with `overlap_tokens = 50`.
  - Each chunk is scored in a forward pass; `merge_token_scores_from_chunks()` averages token probabilities across overlapping character spans `(abs_start, abs_end)` and takes `max(chunk_scores)` for the document-level rerank score.

#### (b) Model & Architecture (`model_structure.py`, `config.json`)
- **Backbone:** `Qwen/Qwen3-Reranker-0.6B` (`Qwen3ForCausalLM`, 28 layers, `hidden_size = 1024`, 16 query heads / 8 KV heads, `head_dim = 128`, `vocab_size = 151669`).
- **Parameter Count (`config.json` & HF metadata):** 595,776,512 BF16 parameters (backbone) + 38,564,870 FP32 parameters (fusion + CRF head) = **634,341,382 total parameters** (~1.35 GB on disk in `model.safetensors`).
- **Multi-Layer Feature Fusion (`Qwen3StopWordModelV2`, `model_structure.py` lines 169–317):**
  - Extracts hidden states from three transformer layers:
    - Early layer: $\lfloor 28 \times 0.25 \rfloor = \text{layer } 7$ (`early_layer_ratio = 0.25`)
    - Middle layer: $\lfloor 28 \times 0.50 \rfloor = \text{layer } 14$ (`middle_layer_ratio = 0.5`)
    - Final layer: $\text{layer } 28$ (`hidden_states[-1]`)
  - Concatenates the three layer representations along the channel dimension: $H_{\text{cat}} \in \mathbb{R}^{B \times L \times 3072}$ (`fused_hidden_size = 3072`).
  - Applies `num_fusion_layers = 1` self-attention block: `nn.MultiheadAttention(embed_dim=3072, num_heads=8)` + residual connection + `LayerNorm(3072)` + `Dropout(0.4)`.
  - *Paper vs. Code Note:* Paper Appendix B.1 states the fusion block uses "8 attention heads and a hidden size of 256"; in the released `model_structure.py` and `config.json`, the 8-head MHA operates directly on `fused_hidden_size = 3072`, while `bottleneck = 256` is the intermediate dimension of the MLP inside `CRFCompressionHead` (`LayerNorm(3072) -> Linear(3072, 256) -> GELU -> Dropout(0.4) -> Linear(256, 2)`).
- **Dual Output Heads:**
  1. **Token Compression Head (`CRFCompressionHead`, `model_structure.py` lines 88–137):** Maps fused states to 2-class emission logits $E \in \mathbb{R}^{B \times L \times 2}$ (`0 = prune`, `1 = keep`) paired with a 2-state linear-chain Conditional Random Field (`CRFLayer(num_tags=2)`) with learnable transition matrix $T \in \mathbb{R}^{2 \times 2}$, `start_transitions`, and `end_transitions`.
  2. **Document Reranking Head (`model_structure.py` lines 319–332):** Projects the final-layer last-token hidden state through `embedding_layer.weight.T`, extracts the logits for the `"no"` and `"yes"` tokens, computes `log_softmax([no, yes])`, and outputs $s_{\text{doc}} = \exp(\text{log\_softmax}_{\text{yes}}) \in [0, 1]$.

#### (c) Scoring, Line Aggregation & Thresholds (Including Paper-vs-Code Discrepancy)
- **Token-Level Scoring (`model_structure.py` lines 371–374, `prune_wrapper.py` lines 513–525):**
  - *Paper description (Section 3.3 & Appendix B.2):* States that the CRF layer performs Viterbi decoding at inference time and averages token scores per line.
  - *Actual released inference code (`model_structure.py` line 372 & `prune_wrapper.py` line 521):* When `labels=None`, `Qwen3StopWordModelV2.forward()` returns raw emission logits `emissions` without calling `self.compression_head.decode()` (Viterbi). `prune_wrapper.py` computes the logit margin between keep (`1`) and prune (`0`):
    $$z_i = E_{i, 1} - E_{i, 0}, \qquad s_i = \sigma(z_i) = \frac{1}{1 + \exp(-(E_{i, 1} - E_{i, 0}))} \in (0, 1)$$
    (which is mathematically identical to $\text{softmax}(E_i)_1$). Thus, the CRF transition potentials regularize the emission head during training, while inference runs fast parallel marginal sigmoid scoring without sequential Viterbi overhead.
- **Token-to-Line Aggregation (`aggregate_token_scores_to_lines`, `prune_wrapper.py` lines 197–260):**
  - Only tokens whose character offset span `[start, end)` falls inside the `<Document>: {code}` substring (`code_start_char` to `code_end_char`) are kept.
  - Each token's probability $s_i$ is assigned to every character index $c \in [\text{rel\_start}, \text{rel\_end})$ it covers (`char_scores[c].append(score)`).
  - For each line $j$ spanning character range $[\text{line\_start}_j, \text{line\_end}_j)$, the line score $\bar{s}_j$ is the mean of all character scores in that line (lines with no token coverage receive `0.0`).
- **Thresholds & Post-Processing (`prune_code_lines`, `prune_wrapper.py` lines 262–313):**
  - **Line keep threshold:** Default $\tau = 0.5$ (`threshold: float = 0.5` in `PruneRequest` and `prune()`). Lines with $\bar{s}_j \ge \tau$ are marked kept.
  - **Single-line gap smoothing (`prune_wrapper.py` lines 284–291):** If two kept lines $k_{\text{prev}}$ and $k$ are separated by exactly one pruned line ($k - k_{\text{prev}} == 2$), the intermediate line $k - 1$ is automatically added to `keep_lines`.
  - **Omission marker length check (`prune_wrapper.py` lines 298–308):** Contiguous pruned line spans of length $N$ are replaced with `(filtered N lines)` **only if** the total character count of those $N$ lines exceeds `len("(filtered 0 lines)") == 18`; otherwise, the original source lines are kept verbatim to avoid expanding token count.

#### (d) Training Data, Teacher Pipeline & Loss Objective
- **Corpus Source:** Sampled 200,000 code snippets (195,370 files from 5,945 repositories) from `nick007x/github-code-2025` (opened <https://huggingface.co/api/datasets/nick007x/github-code-2025>, accessed 2026-10-19).
- **9-Task Agentic Query Synthesis:** Using `Qwen3-Coder-30B-A3B-Instruct` (`temperature = 0.7`, `top_p = 0.9`), synthesized task queries and line-level retention intervals (`kept_frags`, 1-based `[start, end]` line ranges) across 9 task types: `code-summarize`, `code-refactor`, `find-relevant-part`, `code-locate`, `code-optimize`, `code-explain`, `code-debug`, `feature-addition`, and `code-completion`.
- **LLM Judge Filtering:** `Qwen3-Next-80B-A3B-Thinking` audited candidates across three criteria (query realism, deletion relevance, and syntactic structure preservation such as keeping `if/else` headers), rejecting ~5/6 of samples and yielding **61,184 training instances** (`swe-pruner-training-dataset-py.jsonl`).
- **Multi-Task Training Objective (`model_structure.py` lines 346–369):**
  $$\mathcal{L} = (1 - \lambda)\,\mathcal{L}_{\text{prune}} + \lambda\,\mathcal{L}_{\text{rerank}}, \qquad \lambda = 0.05$$
  where $\mathcal{L}_{\text{prune}} = -\frac{1}{|C|}\log P_{\text{CRF}}(y \mid E)$ is the sequence-length-normalized CRF negative log-likelihood over code tokens (`compression_mask`), and $\mathcal{L}_{\text{rerank}} = (s_{\text{doc}} - s_{\text{ref}})^2$ is the MSE against the teacher reranker score.
- **Training Hyperparameters (`train/README.md` & Paper Section 4.1):** 3 epochs, global batch size 128 (`16 per GPU × 8 GPUs`), AdamW (`weight_decay = 0.01`, `dropout = 0.4`, learning rate `3e-5` in paper / `1e-4` in `train/README.md`), freezing the lower 26 transformer layers and unfreezing only the top 2 backbone layers (`--num-finetune-layers 2`) plus the fusion and CRF heads.

---

### 1.2 LongCodeZip (arXiv:2510.00446)

- **Primary Sources Opened (all accessed 2026-10-19):**
  - Paper abstract & HTML: <https://arxiv.org/abs/2510.00446>, <https://arxiv.org/html/2510.00446v1>
  - Repository (`main` branch): <https://github.com/YerbaPage/LongCodeZip>, <https://raw.githubusercontent.com/YerbaPage/LongCodeZip/main/README.md>
  - Implementation (`LongCodeZip` class & `EntropyChunking`): <https://raw.githubusercontent.com/YerbaPage/LongCodeZip/main/longcodezip/__init__.py>, <https://raw.githubusercontent.com/YerbaPage/LongCodeZip/main/demo.py>

#### (a) Inputs & Models
- **Inputs:**
  1. `context` ($C$): Background code context (one or more files/functions).
  2. `question` ($q$): Task query, instruction, or incomplete target code prefix/suffix.
  3. `rate` ($R \in (0, 1]$) or `target_token` ($B$): Target compression budget.
- **Models (`longcodezip/__init__.py` lines 163–180, Paper Section 4.1):**
  - **Training-free:** Requires **no fine-tuning or custom heads**—uses standard causal language modeling log-probabilities from an off-the-shelf pretrained code LLM.
  - Default compressor / perplexity model in paper and code: `Qwen/Qwen2.5-Coder-7B-Instruct` (main experiments) and `Qwen/Qwen2.5-Coder-0.5B-Instruct` (used as the fast perplexity-chunking model in `EntropyChunking`, `longcodezip/__init__.py` line 15, and evaluated in the paper's compressor-scale ablation across 0.5B, 1.5B, 3B, 7B, and 14B backbones).

#### (b) Stage 1: Coarse-Grained Compression (Function/Class Selection)
1. **Top-Level Chunking (`split_code_by_functions`, `longcodezip/__init__.py` lines 555–627):**
   Splits source files along `def` and `class` boundaries (matching top-level or minimally indented definitions via regex `(?:^|\n)(\s*)(?:def|class)\s+[^\n]+`, while keeping file-level header/imports before the first match as chunk 0).
2. **Conditional Perplexity / Approximated Mutual Information (`get_condition_ppl`, lines 284–356):**
   First computes the unconditional perplexity of the query $\text{PPL}(q)$ once. Then, for each candidate chunk $c_i$, concatenates `c_i + "\n\n" + q` and computes the conditional perplexity $\text{PPL}(q \mid c_i)$ by masking the cross-entropy loss to only the tokens belonging to $q$ (`condition_mode = "prefix"`):
   $$\text{AMI}(c_i, q) = \text{PPL}(q) - \text{PPL}(q \mid c_i)$$
   where $\text{PPL}(q \mid c_i) = \exp\!\left(-\frac{1}{|q|}\sum_{t \in q} \log P_\theta(t \mid c_i, q_{<t})\right)$. Chunks that reduce the model's surprise on $q$ have positive $\text{AMI}(c_i, q)$.
3. **Coarse Budget Selection (`control_context_budget`, lines 369–475):**
   Ranks chunks in descending order of $\text{AMI}(c_i, q)$ and greedily selects chunks until reaching the coarse token budget (`context_budget = "+100"` over `target_token` in code, or $B / R_{\text{fine}}$ in the paper). Selected chunks are restored to their **original file order** (`reorder_context = "original"`), and deselected chunks are replaced with a single `# ...` omission marker (`collapse_consecutive_omissions = True`).

#### (c) Stage 2: Fine-Grained Compression (Intra-Function Block Pruning via 0/1 Knapsack)
1. **Perplexity-Spike Intra-Function Chunking (`EntropyChunking`, lines 12–155):**
   - Splits a retained function $f_i$ into individual lines $s_1, \dots, s_m$ (converting blank-line breaks `\n\n` into single-space marker lines `' '`).
   - Computes the conditional base-2 perplexity of each line given all preceding lines in the function:
     $$\text{PPL}_2(s_k \mid s_{<k}) = \log_2 \exp\!\left(-\frac{1}{|s_k|}\sum_{t \in s_k} \log P_\theta(t \mid s_{<k}, t_{<\cdot})\right)$$
   - Identifies local perplexity spikes (`find_spike_indices`, lines 76–104): line $k$ is a spike if $\text{PPL}_2(s_k) \ge \text{PPL}_2(s_{k-1})$, $\text{PPL}_2(s_k) \ge \text{PPL}_2(s_{k+1})$, and at least one neighbor difference is $\ge \theta_{\text{spike}}$, where:
     $$\theta_{\text{spike}} = \mu_{\text{PPL}} + k_{\text{std}}\,\sigma_{\text{PPL}} \qquad (k_{\text{std}} = 0.2 \text{ in code; } \alpha\sigma \text{ in paper})$$
   - Starts a new semantic code block after each spike index (`split_indices = [idx + 1 for idx in spike_indices]`).
   - *Implementation Bottleneck Identified in Upstream Code (`calculate_sentence_ppl`, lines 59–74):* Upstream `LongCodeZip` runs a separate `model(input_ids)` forward pass for every prefix `sentences[:k]` ($O(m^2)$ work per function!). Because causal self-attention is already lower-triangular, all $m$ line perplexities $\text{PPL}_2(s_k \mid s_{<k})$ can be computed in **one single forward pass** over the function by slicing per-token cross-entropy losses by line offset.
2. **Importance-Aware Adaptive Budget Allocation (`compress_code_file`, lines 928–980, Paper Eq. 3–6):**
   - Functions with fewer than `min_lines_for_fine_grained = 5` lines bypass fine-grained pruning ($R_i = 1.0$).
   - For larger functions ($\ge 5$ lines), coarse AMI scores are min-max normalized across the retained set to $\text{AMI}_{\text{norm}, i} \in [0, 1]$ (with `1e-8` epsilon).
   - Given base retention ratio $R_{\text{base}}$ over large functions and sensitivity $\beta = 0.5$ (`importance_beta = 0.5`), each function receives an individual retention ratio:
     $$R_i = \text{clamp}_{[0, 1]}\!\Big(R_{\text{base}} \cdot \big(1 + 2\beta\,(\text{AMI}_{\text{norm}, i} - 0.5)\big)\Big)$$
     followed by a global normalization factor so $\sum_i R_i T_i$ equals the remaining target budget $B_{\text{large}}$.
3. **Structural Preservation + Block Scoring (`_compress_Single_function`, lines 660–754):**
   - **Always-Preserved Blocks (`preserved_block_indices`):** Any block whose first non-empty line contains `def `, `class `, `#`, or `return ` is unconditionally preserved and its token cost is deducted from the function's budget $B_i = \lfloor R_i T_i \rfloor$.
   - **Block Scoring:** Remaining blocks $b_{i,k}$ are scored either by `conditional_ppl` ($\text{PPL}(q) - \text{PPL}(q \mid b_{i,k})$, default when `q` is non-empty) or `contrastive_perplexity` ($\text{PPL}(b_{i,k} \mid b_{i,<k}) - \text{PPL}(b_{i,k} \mid q, b_{i,<k})$).
4. **0/1 Knapsack Optimization (`_knapsack_block_selection` & `_solve_knapsack_dp`, lines 756–900):**
   - Maximizes total block importance $\sum_{k} z_{i,k}\,\text{score}(b_{i,k})$ subject to $\sum_{k} z_{i,k}\,\text{tokens}(b_{i,k}) \le B_i - B_{i,\text{preserved}}$, $z_{i,k} \in \{0, 1\}$.
   - Uses exact dynamic programming (`_solve_knapsack_dp`, normalizing negative scores by shifting `min_score` to `0.1`) when `n <= 100` blocks and remaining budget `<= 2000` tokens, falling back to greedy value-density (`score / max(weight, 1)`) otherwise.
   - Selected blocks are reassembled in **original source line order**, with omitted spans replaced by indented `# ...`.

---

## 2. Open Weights, Code, Datasets, and Licenses

All URLs below were directly opened and verified on **2026-10-19**:

| Artifact | Type | URL (Opened 2026-10-19) | Params / Size | Verified License | Notes |
|---|---|---|---|---|---|
| **SWE-Pruner repo** (`Ayanami1314/swe-pruner`) | Code (inference, FastAPI server, agent wrappers, training scripts) | <https://github.com/Ayanami1314/swe-pruner> <br> <https://raw.githubusercontent.com/Ayanami1314/swe-pruner/public/LICENSE> | — | **Apache-2.0** (Copyright 2025 Yuhang Wang) | Default branch is `public`. Includes `swe-pruner` Python package (`requires-python >= 3.12`, `torch >= 2.8.0`, `transformers >= 4.55.2`). |
| **SWE-Pruner weights** (`ayanami-kitasan/code-pruner`) | Open model weights (`model.safetensors` + custom `config.json`) | <https://huggingface.co/ayanami-kitasan/code-pruner> <br> <https://huggingface.co/api/models/ayanami-kitasan/code-pruner> | **634.3M params** (595.8M BF16 + 38.6M FP32; **1.35 GB** file) | **MIT** per the HF API card metadata (`cardData.license: mit`, checked by the coordinator 2026-10-19); the report originally said Apache-2.0 | Trained on Python (`swe-pruner-training-dataset-py.jsonl`) + general code; drop-in loadable via `Qwen3StopWordModelV2.from_pretrained()`. |
| **SWE-Pruner Pro head** (`ayanami-kitasan/swe-pruner-pro-qwen3-coder-next-head`) | Experimental head weights | <https://huggingface.co/api/models/ayanami-kitasan/swe-pruner-pro-qwen3-coder-next-head> | Head-only checkpoint | **No license tag declared** on HF card (treat as unverified/restricted) | Updated 2026-03-04; targets `Qwen3-Coder-Next`, not needed for 0.6B deployment. |
| **Base Backbone** (`Qwen/Qwen3-Reranker-0.6B`) | Pretrained backbone weights | <https://huggingface.co/api/models/Qwen/Qwen3-Reranker-0.6B> | 595.8M params (BF16) | **Apache-2.0** | Base model for `ayanami-kitasan/code-pruner`. |
| **SWE-Pruner Corpus Source** (`nick007x/github-code-2025`) | Raw pre-synthesis dataset | <https://huggingface.co/api/datasets/nick007x/github-code-2025> | ~1.5M repos | **MIT** (`license: mit`) | Note: The synthesized 61,184-sample `swe-pruner-training-dataset-py.jsonl` referenced in `train/README.md` is **not** uploaded to `ayanami-kitasan`'s HF namespace as of 2026-10-19; only the trained weights and training scripts are published. |
| **LongCodeZip repo** (`YerbaPage/LongCodeZip`) | Code (`longcodezip` Python package) | <https://github.com/YerbaPage/LongCodeZip> <br> <https://raw.githubusercontent.com/YerbaPage/LongCodeZip/main/LICENSE> | — | **MIT** (Copyright 2025 YerbaPage) | Training-free library; works with any HuggingFace `AutoModelForCausalLM`. |
| **Local WSL Cached Models** (`Qwen/Qwen2.5-0.5B-Instruct`, `Qwen/Qwen2.5-1.5B-Instruct`) | Pretrained causal LM weights (already on local disk) | Referenced in `evals/b8_skim_offline.py` & `docs/engineering/SKIM.md` | 0.49B (~1.0 GB FP16) / 1.54B (~3.1 GB FP16) | **Apache-2.0** | Already downloaded in WSL (`local_files_only=True` in `evals/b4_v3.py`). |

---

## 3. Training-Free Variants We Can Run Immediately (Local `Qwen2.5-0.5B` / `1.5B`)

Because `Qwen/Qwen2.5-0.5B-Instruct` and `Qwen/Qwen2.5-1.5B-Instruct` are already cached in WSL and fit comfortably in 8 GB VRAM (~1.0 GB and ~3.1 GB in BF16/FP16), we can run three training-free variants immediately without downloading new weights:

### Variant TF-1: Single-Pass KV-Cached LongCodeZip Conditional Perplexity (Recommended Immediate Baseline)
Instead of upstream `LongCodeZip`'s unbatched loop and $O(m^2)$ `EntropyChunking` passes, we combine our existing AST chunker (`kernel/router/skim.py`) with **KV-cached conditional perplexity** (`PPL(objective | block)`) using `Qwen/Qwen2.5-0.5B-Instruct` (or `1.5B`):

1. **Coarse Filter (Hybrid BM25 + AST):**
   Use our fast deterministic `bm25_scores(objective, chunks)` to shortlist the top $K = 12$ AST chunks (covering ~6k–10k tokens, where BM25 chunk recall is already >90% on B4 v3 dev).
2. **Structural Splitting (AST Statements / Blank-Line Paragraphs, Zero LLM Cost):**
   Replace `EntropyChunking`'s $O(m^2)$ LLM forward passes with Python `ast` statement boundaries (keeping `def`/`class` signatures, docstrings, `return`/`raise` headers, and module-level constant assignments intact as atomic blocks).
3. **Batched Conditional-PPL Scoring (`AMI(b, q) = L(q) - L(q | chunk_header, b)`):**
   - Compute baseline query negative log-likelihood (NLL) $L(q) = -\frac{1}{|q|}\sum_{t \in q} \log P_\theta(t \mid q_{<t})$ in a single forward pass (~30 tokens).
   - For each candidate block $b_{i,k}$ inside shortlisted chunk $c_i$, form the short prompt:
     ```text
     # File: {rel} ({symbol_signature})
     {block_text}
     # Question: {objective}
     ```
   - Compute $L(q \mid c_{i,\text{sig}}, b_{i,k})$ restricted to the `# Question: {objective}` suffix tokens in padded mini-batches of 16–32 blocks (`max_len <= 512` tokens).
   - Why this fixes the B8b `SKIM` regression: When a block contains `DEFAULT_MAX_REDIRECTS = 20` or `return Response(status_code=200)` inside `Client._send_handling_redirects`, the causal LM conditioned on `# File: httpx/_client.py (def _send_handling_redirects(...))` + `{block_text}` assigns higher probability to objective tokens asking about redirect limits or response handling—even when the block shares zero exact surface words with the question.
4. **0/1 Knapsack Selection + Single File Header per Chunk:**
   - Allocate per-chunk token budgets via LongCodeZip's importance-biased ratio $R_i$ (Eq. 5) and solve `_solve_knapsack_dp` per chunk.
   - Re-emit surviving blocks in **strictly increasing source line order** under a **single** `===== FILE: <rel> =====` header per chunk (eliminating the per-block header tax that crippled `kernel/router/skim.py`), inserting `# ...` between non-adjacent line spans.

### Variant TF-2: Single-Pass Causal Contrastive Perplexity (Prefix-Cached Full-Chunk Pass)
For longer functions where blocks depend on local variables defined 10 lines earlier (e.g., `url = ...` followed by `return merge_url(url)`):
1. Run **two forward passes per shortlisted AST chunk** $c_i$ (length $L_c \le 1024$ tokens):
   - **Unconditioned pass:** Input `[c_i]` $\rightarrow$ extract per-token log-probs $\log P_\theta(t \mid c_{i,<t})$ for all tokens in $c_i$ simultaneously.
   - **Query-conditioned pass:** Input `[q, "\n\n", c_i]` $\rightarrow$ extract per-token log-probs $\log P_\theta(t \mid q, c_{i,<t})$ for all tokens in $c_i$ simultaneously.
2. Aggregate the token log-likelihood gain per line or block $b_{i,k}$:
   $$\Delta \text{NLL}(b_{i,k} \mid q) = \frac{1}{|b_{i,k}|}\sum_{t \in b_{i,k}} \Big(\log P_\theta(t \mid q, c_{i,<t}) - \log P_\theta(t \mid c_{i,<t})\Big)$$
3. **Why this is fast:** Instead of running 1 forward pass *per block* ($B$ passes per chunk), causal masking yields the exact contrastive perplexity of **every line and block in the chunk in just 2 forward passes per chunk** (24 forward passes total for top-12 chunks!).
4. **Caveat (`LongCodeZip` Paper Section 3.3.3 & Table 5):** Because causal attention is unidirectional, tokens in $b_{i,k}$ can attend to $q$ in the prefix, so $\log P_\theta(t \mid q, c_{i,<t})$ measures how much the query helps predict the code block (`P(code | query)`), whereas `TF-1` measures how much the code block helps predict the query (`P(query | code)`). Combining both scores (`z_norm(TF-1) + z_norm(TF-2)`) gives bidirectional mutual information with causal models.

### Variant TF-3: Last-Layer Query-to-Code Attention Pooling (1 Forward Pass per Chunk)
1. Feed `[c_i, "\n\n# Question: ", q]` into `Qwen2.5-0.5B-Instruct` with `output_attentions=True` (on the final 4 layers only, using `attn_implementation="eager"` on the last layers or computing $Q_q K_c^\top / \sqrt{d}$ hooks on layers 21–24).
2. Average the attention weights from query tokens $t \in q$ to code tokens $t' \in c_i$, max-pool across attention heads, and average per code line.
3. Requires only **1 forward pass per chunk** (12 passes total), though zero-shot causal attention exhibits attention-sink bias toward initial tokens unless normalized by baseline positional attention.

---

## 4. Concrete Build Plan for Context-Fabric

### 4.1 Architecture & Algorithm Recommendation: Two-Track Staged Rollout

We recommend a two-stage engineering plan behind a feature flag (`SkimMode = Literal["bm25", "qwen_ppl", "swe_pruner"]`, default `"bm25"` for strict byte-identity when off):

```
Query (objective) + Corpus (AST Chunks)
   │
   ├─► Stage 0: AST + Module-Assignment Chunker Fix
   │     Include top-level ast.Assign / ast.AnnAssign in module header chunks
   │     (fixes missing constants like DEFAULT_MAX_REDIRECTS in both R and SKIM).
   │
   ├─► Stage 1: Coarse Lexical Filter (BM25 + Symbol Boost)
   │     Rank AST chunks; shortlist top K=12 chunks (~6k–8k tokens).
   │
   ├─► Stage 2: Fine Line/Block Scorer (Pluggable Backend)
   │     ├─► Track A (Zero-Download, Immediate): TF-1 + TF-2 Hybrid Conditional PPL
   │     │     Model: local Qwen/Qwen2.5-0.5B-Instruct (or 1.5B) in BF16.
   │     └─► Track B (Best Accuracy/Latency, 1.35 GB Apache-2.0 download):
   │           Model: ayanami-kitasan/code-pruner (SWE-Pruner 0.6B CRF).
   │           Batch-score shortlisted chunks in 1 forward pass per chunk;
   │           get per-line keep probabilities s_j in [0, 1] + doc rerank score s_doc.
   │
   └─► Stage 3: Budget-Constrained Knapsack Assembler (Source-Order Emission)
         • Always preserve function/class signature line + return/raise headers.
         • Apply single-line gap smoothing (if lines j-1 and j+1 kept, keep j).
         • Select blocks via 0/1 Knapsack DP to hit exact token budget (1k / 2k / 4k).
         • Emit in original source line order under ONE "===== FILE: <rel> ====="
           header per chunk, using "# ..." for omitted spans > 18 chars.
```

#### Why SWE-Pruner 0.6B (Track B) + LongCodeZip Knapsack Assembly Is Optimal for Hand-Offs (B3)
- **Raw SWE-Pruner weakness:** SWE-Pruner uses a fixed threshold $\tau = 0.5$ (`prune_wrapper.py` line 59), which cannot guarantee a strict hard token cap (`budget = 1024 / 2048 / 4096`) required by `compile_node_packet` and `evals/b8_skim_offline.py`.
- **Raw LongCodeZip weakness:** LongCodeZip's fine-grained stage requires many forward passes per function and relies on zero-shot generative perplexity rather than a discriminative keep/prune classifier trained on 61k code tasks.
- **Our Hybrid (`SWE-Pruner` line probabilities + `LongCodeZip` importance budget + 0/1 Knapsack):**
  1. Run `ayanami-kitasan/code-pruner` once per shortlisted AST chunk $c_i$ to obtain both the document relevance score $s_{\text{doc}, i} \in [0, 1]$ and per-line probabilities $\bar{s}_{i, j} \in [0, 1]$.
  2. Group contiguous AST statement blocks $b_{i,k}$ and set block value $v_{i,k} = \sum_{j \in b_{i,k}} \bar{s}_{i,j}$ (weighted by $s_{\text{doc}, i}$) and weight $w_{i,k} = \text{tokens}(b_{i,k})$.
  3. Solve `_solve_knapsack_dp` across the shortlisted chunks for the exact target budget $B \in \{1024, 2048, 4096\}$, preserving signatures and emitting blocks in source order under one `===== FILE: <rel> =====` header per chunk.

### 4.2 Expected Memory & Latency on Local 8 GB RTX 5070 (WSL2)

- **Hardware Specs (Reference):** NVIDIA RTX 5070 Laptop/Desktop 8 GB GDDR7 (~384–448 GB/s memory bandwidth, Blackwell FP16/BF16 Tensor Cores).
- **VRAM Footprint & Latency Estimates** *(Note: All RTX 5070 timing figures below are `[UNVERIFIED / EXPLORATORY ESTIMATES]` extrapolated from model FLOPs, memory bandwidth, and SWE-Pruner Paper Section 4.4 / Table 4 reported latencies of 38 ms at 500 tokens to 156 ms at 2,000 tokens on H200; must be benchmarked locally via `evals/b8_skim_offline.py`)*:

| Backend Option | Weights VRAM (BF16) | Peak VRAM (`batch=8`, `seq<=1024`) | Forward Passes per Objective (Top-12 Chunks, ~6k tokens total) | Expected Latency per Objective on RTX 5070 8 GB `[EXPLORATORY ESTIMATE]` | Fits 8 GB Alongside Other Processes? |
|---|---:|---:|---:|---:|---|
| **BM25 (`R` / `SKIM`)** | 0 MB (CPU) | 0 MB | 0 | **~2–5 ms** (measured CPU) | Yes |
| **Track A1: `Qwen2.5-0.5B-Instruct` (TF-2 Contrastive PPL, 2 passes/chunk)** | ~1.0 GB | ~1.6 GB | 24 chunk passes (batched as $3 \times 8$) | **~90–180 ms** `[EXPLORATORY]` | Yes (~6.4 GB free) |
| **Track A2: `Qwen2.5-0.5B-Instruct` (TF-1 Block Conditional PPL)** | ~1.0 GB | ~1.8 GB | ~80 short block passes (batched as $3 \times 32$, `len<=384`) | **~120–240 ms** `[EXPLORATORY]` | Yes (~6.2 GB free) |
| **Track A3: `Qwen2.5-1.5B-Instruct` (TF-2 Contrastive PPL)** | ~3.1 GB | ~4.2 GB | 24 chunk passes (batched as $3 \times 8$) | **~220–420 ms** `[EXPLORATORY]` | Yes (~3.8 GB free) |
| **Track B: `ayanami-kitasan/code-pruner` (SWE-Pruner 0.6B CRF)** | ~1.35 GB | ~2.1 GB | 12 chunk passes (batched as $2 \times 6$, `len<=1024`) | **~70–150 ms** `[EXPLORATORY]` | Yes (~5.9 GB free) |

- **Serving / Runtime Choice (`transformers` vs `vLLM`):**
  - **Use PyTorch + `transformers` (`sdpa` / FlashAttention-2) in-process or via a lightweight local sidecar**, **not** `vLLM`, for the skimmer:
    1. `SWE-Pruner` (`Qwen3StopWordModelV2`) extracts intermediate hidden states from layers 7, 14, and 28 (`output_hidden_states=True`) and runs a custom `MultiheadAttention` + `CRFCompressionHead`. Upstream `swe-pruner` serves this via FastAPI + PyTorch (`online_serving.py`), not `vLLM`, because `vLLM` does not expose intermediate layer hidden states (`0.25L`, `0.5L`) to custom token-classification heads.
    2. For `LongCodeZip` (`TF-1`/`TF-2`), we only need prefill log-probabilities (`logits` / `CrossEntropyLoss(reduction='none')`), with `max_new_tokens = 0` (no autoregressive decoding loop). Standard `torch.inference_mode()` with `attn_implementation="sdpa"` in BF16 saturates GPU compute on batch prefill without `vLLM`'s KV-cache slab reservation (~90% VRAM by default, which would OOM or starve an 8 GB card).

### 4.3 Offline Evaluation Plan Against BM25 Baseline (`evals/b8_skim_offline.py`)

All evaluation runs strictly offline against `evals/fixtures/b4v3_objectives.json` (20 dev objectives, 33 required facts across 23 `httpx` files) with **zero OpenAI/API calls**:

1. **Structural Fixes & Ablation Arms to Add Behind `--backend` in `evals/b8_skim_offline.py` (when implementation is scheduled):**
   - `R`: Existing whole-AST-chunk BM25 baseline (19/33 @ 1k, 27/33 @ 2k, 28/33 @ 4k).
   - `SKIM_V0`: Current `kernel/router/skim.py` (18/33 @ 1k, 23/33 @ 2k, 23/33 @ 4k).
   - `SKIM_STRUCT`: Pure structural fix (CPU-only: single `===== FILE: <rel> =====` header per chunk + source-order emission + module-level `ast.Assign` constant inclusion + keep zero-BM25 body blocks when chunk budget permits). Isolates how much of the -5 fact gap was header tax + block dropping vs. lexical scoring.
   - `SKIM_QWEN_05B_PPL`: Track A (`Qwen/Qwen2.5-0.5B-Instruct`, `local_files_only=True`, hybrid `TF-1`/`TF-2` block scoring + 0/1 Knapsack).
   - `SKIM_QWEN_15B_PPL`: Track A with `Qwen/Qwen2.5-1.5B-Instruct` (`local_files_only=True`).
   - `SKIM_SWE_PRUNER_06B`: Track B (`ayanami-kitasan/code-pruner` 0.6B line probabilities + document rerank score + 0/1 Knapsack).
2. **Primary Metrics & Acceptance Gates (`evals/b8_skim_offline.py`):**
   - **Fact Recall (`facts_hit / 33`)** and **Objective All-Fact Pass Rate (`objs_all_hit / 20`)** at budgets `1024`, `2048`, and `4096` tokens (counted with the exact same `_Counter` / `Qwen2.5-0.5B-Instruct` tokenizer used in `evals/b8_skim_offline.py`).
   - **Promotion Gate (from `docs/engineering/SKIM.md`):**
     - Must strictly beat `R` on `facts_hit` at both **1,024 tokens (> 19/33)** and **2,048 tokens (> 27/33)**, and be non-inferior at **4,096 tokens ($\ge$ 28/33)**.
     - Must have **zero regressions** on objectives where `R` hits all facts (per-objective paired check).
     - Wall-clock p95 latency per objective $\le 500\text{ ms}$ on the local RTX 5070 8 GB.

### 4.4 Risks and Mitigations

| Risk | Severity | Mechanism | Concrete Mitigation |
|---|---|---|---|
| **1. SWE-Pruner Fixed Threshold ($\tau=0.5$) Violates Hard Token Budgets** | High | `prune_wrapper.py` keeps all lines with $\bar{s}_j \ge 0.5$, which may output 3,200 tokens when `budget = 1024` or 300 tokens when `budget = 4096`. | Replace fixed thresholding with LongCodeZip's 0/1 Knapsack DP (`_solve_knapsack_dp`) over AST statement blocks using SWE-Pruner's continuous line scores $\bar{s}_j$ as item values and token counts as item weights. |
| **2. Module-Level Constants Invisible to AST Chunker** | High | Both `build_ast_chunks` (`evals/b4_v3.py`) and `_parse_file` (`kernel/router/skim.py`) extract only module docstring + `import` statements into the `:header` chunk and only `FunctionDef`/`AsyncFunctionDef`/`ClassDef` into symbol chunks, silently dropping top-level `ast.Assign` / `ast.AnnAssign` constants (e.g., `DEFAULT_MAX_REDIRECTS = 20`, status/error tables). | Include top-level `ast.Assign` and `ast.AnnAssign` nodes in module header chunks (or dedicated `:constants` chunks) behind the new skimmer flag. |
| **3. Python Version & Dependency Drift in Upstream `swe-pruner`** | Medium | Upstream `swe-pruner/pyproject.toml` pins `requires-python >= 3.12`, `torch >= 2.8.0`, `transformers >= 4.55.2`, and `flash-attn`. | Do **not** install the `swe-pruner` package as a dependency. Copy only the self-contained `Qwen3StopWordModelV2` / `CRFCompressionHead` / `CRFLayer` classes (~280 LoC of pure PyTorch + `transformers`, Apache-2.0 licensed) with `attn_implementation="sdpa"`, which runs cleanly on standard PyTorch 2.4+ / `transformers` 4.51+ without `flash-attn` compilation on WSL. |
| **4. Unidirectional Causal Bias in Training-Free Perplexity (`TF-2`)** | Medium | In `TF-2` (`P(code | query)`), boilerplate tokens (e.g., `self.`, `return None`) already have low entropy and show small $\Delta \text{NLL}$, whereas rare identifier names dominate. | Always preserve structural headers (`def`, `class`, `return`, `raise`) unconditionally (as LongCodeZip does in `preserved_block_indices`) and score interior logic blocks with `TF-1` (`P(query | chunk_sig, block)`), where the query is the prediction target. |
| **5. 8 GB VRAM Contention During Live Runs** | Medium | If another local model or embeddings server is resident on the RTX 5070, a 1.5B model in FP16 (~3.1 GB weights + KV cache) can trigger CUDA OOM or shared-memory paging across PCIe in WSL2. | Default to `Qwen2.5-0.5B-Instruct` (~1.0 GB BF16) for Track A or `ayanami-kitasan/code-pruner` (~1.35 GB BF16) for Track B, enforce `torch.inference_mode()`, cap micro-batch size at `8` chunks (`max_length = 1024`), and set `PYTORCH_CUDA_alloc_conf=expandable_segments:True`. |

---

## 5. Summary of Cited URLs (All Opened & Verified on 2026-10-19)

1. SWE-Pruner arXiv abstract (accessed 2026-10-19): <https://arxiv.org/abs/2601.16746>
2. SWE-Pruner arXiv full HTML paper (accessed 2026-10-19): <https://arxiv.org/html/2601.16746v1>
3. SWE-Pruner GitHub repository & metadata (accessed 2026-10-19): <https://github.com/Ayanami1314/swe-pruner>, <https://api.github.com/repos/Ayanami1314/swe-pruner>
4. SWE-Pruner README & Apache-2.0 License (accessed 2026-10-19): <https://raw.githubusercontent.com/Ayanami1314/swe-pruner/public/README.md>, <https://raw.githubusercontent.com/Ayanami1314/swe-pruner/public/LICENSE>
5. SWE-Pruner package & training READMEs (accessed 2026-10-19): <https://raw.githubusercontent.com/Ayanami1314/swe-pruner/public/swe-pruner/README.md>, <https://raw.githubusercontent.com/Ayanami1314/swe-pruner/public/train/README.md>, <https://raw.githubusercontent.com/Ayanami1314/swe-pruner/public/swe-pruner/pyproject.toml>
6. SWE-Pruner model & inference source files (accessed 2026-10-19):
   - <https://raw.githubusercontent.com/Ayanami1314/swe-pruner/public/swe-pruner/src/swe_pruner/configuration.py>
   - <https://raw.githubusercontent.com/Ayanami1314/swe-pruner/public/swe-pruner/src/swe_pruner/model_structure.py>
   - <https://raw.githubusercontent.com/Ayanami1314/swe-pruner/public/swe-pruner/src/swe_pruner/prune_wrapper.py>
   - <https://raw.githubusercontent.com/Ayanami1314/swe-pruner/public/swe-pruner/src/swe_pruner/swepruner.py>
   - <https://raw.githubusercontent.com/Ayanami1314/swe-pruner/public/swe-pruner/src/swe_pruner/online_serving.py>
7. SWE-Pruner HuggingFace model card, config & metadata (accessed 2026-10-19): <https://huggingface.co/ayanami-kitasan/code-pruner>, <https://huggingface.co/ayanami-kitasan/code-pruner/raw/main/README.md>, <https://huggingface.co/ayanami-kitasan/code-pruner/raw/main/config.json>, <https://huggingface.co/api/models/ayanami-kitasan/code-pruner>
8. Related HuggingFace models & datasets metadata (accessed 2026-10-19): <https://huggingface.co/api/models/ayanami-kitasan/swe-pruner-pro-qwen3-coder-next-head>, <https://huggingface.co/api/models/Qwen/Qwen3-Reranker-0.6B>, <https://huggingface.co/api/datasets/nick007x/github-code-2025>
9. LongCodeZip arXiv abstract (accessed 2026-10-19): <https://arxiv.org/abs/2510.00446>
10. LongCodeZip arXiv full HTML paper (accessed 2026-10-19): <https://arxiv.org/html/2510.00446v1>
11. LongCodeZip GitHub repository, README, MIT License & source code (accessed 2026-10-19):
    - <https://github.com/YerbaPage/LongCodeZip>
    - <https://api.github.com/repos/YerbaPage/LongCodeZip>
    - <https://raw.githubusercontent.com/YerbaPage/LongCodeZip/main/README.md>
    - <https://raw.githubusercontent.com/YerbaPage/LongCodeZip/main/LICENSE>
    - <https://raw.githubusercontent.com/YerbaPage/LongCodeZip/main/demo.py>
    - <https://raw.githubusercontent.com/YerbaPage/LongCodeZip/main/longcodezip/__init__.py>
