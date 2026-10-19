"""Model-based hand-off skimmers (exploratory, local GPU).

Two micro-model tracks behind flags, both feeding one source-order 0/1 knapsack
assembler over AST blocks:

* ``ppl``    — Track A (training-free, LongCodeZip-style): conditional perplexity of the
  objective given each code block (AMI), from ``Qwen/Qwen2.5-0.5B-Instruct`` (causal LM).
* ``pruner`` — Track B: per-line keep-probabilities from ``ayanami-kitasan/code-pruner``
  (SWE-Pruner 634M token-classification, MIT), averaged over each block.

Both keep ``def``/``class`` signature lines, ``return``/``raise`` lines, and import lines
that bind names used in kept lines, then fill the remaining budget with the exact 0/1
knapsack DP, re-emitting every block in source order.

``torch``/``transformers`` are imported only inside the two scorer factories below. The
module and the ``ModelSkimmer`` assembler stay pure Python, so the core package and its
tests never require torch; ``tests/test_skim_model.py`` drives the assembler with a stub
scorer (no GPU).
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Sequence
from pathlib import Path

from kernel.cartridge.serializer import count_tokens
from kernel.router.skim import (
    _Block,
    _build,
    _Chunk,
    _FileInfo,
    _identifiers,
    _rank_chunks,
    _split_blocks,
    trim_to_budget,
)

TokenCounter = Callable[[str], int]

# A block is ``(rel, lines)``; a scorer returns one relevance score per block, aligned with
# ``blocks``. Positive is "more relevant"; absolute scale is irrelevant (knapsack shifts).
BlockScorer = Callable[[str, Sequence[tuple[str, Sequence[str]]]], list[float]]

TOP_K = 12
_CONSTANTS_NAME = "__constants__"
_SEP = "\n\n"


# ---- chunking (AST + module-level constants) ------------------------------------------


def _constants_chunk(rel: str, tree: ast.Module, text: str) -> _Chunk | None:
    """Top-level ``Assign``/``AnnAssign`` as one chunk (dropped by the B4 v3 chunker)."""
    lines: list[str] = []
    start = 0
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            segment = ast.get_source_segment(text, node)
            if segment:
                if not lines:
                    start = node.lineno
                lines.extend(segment.splitlines())
    return _Chunk(rel, _CONSTANTS_NAME, start, tuple(lines)) if lines else None


def _build_model(root: Path, files: Sequence[str]) -> tuple[list[_Chunk], dict[str, _FileInfo]]:
    """``skim._build`` plus a per-file module-constants chunk."""
    chunks, infos = _build(root, files)
    extra: list[_Chunk] = []
    for rel in sorted(files):
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        const = _constants_chunk(rel, tree, text)
        if const is not None:
            extra.append(const)
    chunks.extend(extra)
    return chunks, infos


def _chunk_blocks(chunk: _Chunk, infos: dict[str, _FileInfo]) -> list[tuple[_Block, bool]]:
    """Split a chunk into blocks; ``True`` marks a structurally-forced block.

    Forced = ``def``/``class`` signature line and ``return``/``raise`` blocks. Header chunks
    contribute only their docstring (imports are added later by the binding rule).
    """
    if chunk.is_header:
        info = infos[chunk.rel]
        if info.doc_lines:
            return [(_Block(info.rel, info.doc_line, info.doc_lines), False)]
        return []
    if chunk.name == _CONSTANTS_NAME:
        # Each module-level assignment is its own block (single-line in practice).
        return [
            (_Block(chunk.rel, chunk.start_line + i, (line,)), False)
            for i, line in enumerate(chunk.lines)
            if line.strip()
        ]
    signature = _Block(chunk.rel, chunk.start_line, (chunk.lines[0],))
    out: list[tuple[_Block, bool]] = [(signature, True)]
    for block in _split_blocks(chunk.rel, chunk.lines[1:], chunk.start_line + 1):
        first = next((line for line in block.lines if line.strip()), "")
        out.append((block, first.lstrip().startswith(("return", "raise"))))
    return out


# ---- 0/1 knapsack ---------------------------------------------------------------------


def _knapsack_select(values: Sequence[float], weights: Sequence[int], budget: int) -> list[int]:
    """Exact 0/1 knapsack: maximize total value with total weight <= budget.

    Values are shifted non-negative internally (the DP needs a monotone objective; the
    shift cancels). Returns the indices of the chosen items, ascending.
    """
    n = len(values)
    if budget <= 0 or n == 0:
        return []
    shift = min(values)
    shifted = [v - shift for v in values]
    prev = [0.0] * (budget + 1)
    keep: list[bytearray] = []
    for i in range(n):
        wi = weights[i]
        row = bytearray(budget + 1)
        cur = prev[:]
        if wi <= budget:
            vi = shifted[i]
            for w in range(wi, budget + 1):
                cand = prev[w - wi] + vi
                if cand > cur[w]:
                    cur[w] = cand
                    row[w] = 1
        keep.append(row)
        prev = cur
    taken: list[int] = []
    w = budget
    for i in range(n - 1, -1, -1):
        if keep[i][w]:
            taken.append(i)
            w -= weights[i]
    taken.reverse()
    return taken


# ---- assembler ------------------------------------------------------------------------


class ModelSkimmer:
    """Deterministic model-based skimmer: chunks once, scores per objective, knapsacks per
    budget. ``scorer`` is injectable so tests drive the assembler without torch."""

    def __init__(
        self,
        root: str | Path,
        files: Sequence[str],
        *,
        scorer: BlockScorer | None = None,
        counter: TokenCounter = count_tokens,
        top_k: int = TOP_K,
    ) -> None:
        self.root = Path(root)
        self.files = tuple(sorted(files))
        self.counter = counter
        self.top_k = top_k
        self.scorer = scorer
        self.chunks, self.infos = _build_model(self.root, self.files)
        self._blocks_cache: dict[str, tuple[list[tuple[_Block, bool]], list[float]]] = {}

    def skim(self, objective: str, *, budget: int) -> str:
        if budget <= 0:
            return ""
        cached = self._blocks_cache.get(objective)
        if cached is None:
            # Module constants are few and small: always scored, never coarse-filtered out.
            constants = [c for c in self.chunks if c.name == _CONSTANTS_NAME]
            others = [c for c in self.chunks if c.name != _CONSTANTS_NAME]
            ranked = constants + _rank_chunks(objective, others)[: self.top_k]
            blocks: list[tuple[_Block, bool]] = []
            for chunk in ranked:
                blocks.extend(_chunk_blocks(chunk, self.infos))
            scores = self._score(objective, blocks)
            self._blocks_cache[objective] = (blocks, scores)
        else:
            blocks, scores = cached

        items: list[tuple[_Block, float, int, bool]] = []
        for (block, is_forced), value in zip(blocks, scores, strict=True):
            items.append((block, value, self.counter(block.render()), is_forced))

        forced_items = [it for it in items if it[3]]
        free = [it for it in items if not it[3]]
        spent = sum(it[2] for it in forced_items)
        avail = budget - spent
        chosen = _knapsack_select([it[1] for it in free], [it[2] for it in free], avail)
        kept = [it[0] for it in forced_items] + [free[i][0] for i in chosen]

        kept = self._bind_imports(kept)
        return self._render(kept, budget)

    def _score(self, objective: str, blocks: Sequence[tuple[_Block, bool]]) -> list[float]:
        """Per-block scores (0.0 everywhere when no scorer is configured)."""
        if self.scorer is None or not blocks:
            return [0.0] * len(blocks)
        return self.scorer(objective, [(b.rel, b.lines) for b, _forced in blocks])

    def _bind_imports(self, kept: Sequence[_Block]) -> list[_Block]:
        """Always keep import lines that bind names used in the kept lines."""
        used_by_file: dict[str, set[str]] = {}
        for block in kept:
            used_by_file.setdefault(block.rel, set()).update(_identifiers(block.lines))
        out = list(kept)
        seen = {(b.rel, b.start_line) for b in out}
        for rel in sorted(self.infos):
            used = used_by_file.get(rel, set())
            for imp in self.infos[rel].imports:
                if (rel, imp.start_line) in seen:
                    continue
                if set(imp.bound) & used:
                    out.append(_Block(rel, imp.start_line, tuple(imp.text.splitlines())))
        return out

    def _render(self, kept: Sequence[_Block], budget: int) -> str:
        ordered = sorted(kept, key=lambda b: (b.rel, b.start_line))
        parts: list[str] = []
        spent = 0
        for block in ordered:
            text = block.render()
            cost = self.counter(text)
            sep = self.counter(_SEP) if parts else 0
            if spent + sep + cost > budget:
                continue
            parts.append(text)
            spent += sep + cost
        text = _SEP.join(parts)
        # Safety net: render-loop separators can push over budget; trim trailing lines.
        if self.counter(text) > budget:
            text = trim_to_budget(text, budget, self.counter)
        return text


# ---- lazy scorer factories ------------------------------------------------------------


def ppl_scorer(model_name: str = "Qwen/Qwen2.5-0.5B-Instruct") -> BlockScorer:
    """Track A: contrastive perplexity, PPL(block) - PPL(block | objective) per block."""
    import torch  # lazy: core package never imports torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_name, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token_id = tokenizer.eos_token_id
    model = AutoModelForCausalLM.from_pretrained(
        model_name, local_files_only=True, dtype=torch.bfloat16
    )
    model = model.to("cuda")
    model.eval()

    def next_logprobs(logits: torch.Tensor, ids: torch.Tensor) -> torch.Tensor:
        lse = torch.logsumexp(logits, dim=-1)
        target = logits[:, :-1].gather(2, ids[:, 1:].unsqueeze(-1)).squeeze(-1)
        return target - lse[:, :-1]

    def score(objective: str, blocks: Sequence[tuple[str, Sequence[str]]]) -> list[float]:
        q_ids = tokenizer(objective, add_special_tokens=False)["input_ids"]
        sep_ids = tokenizer(_SEP, add_special_tokens=False)["input_ids"]
        base = len(q_ids) + len(sep_ids)
        if not q_ids or not blocks:
            return [0.0] * len(blocks)

        block_ids = [
            tokenizer("\n".join(lines), add_special_tokens=False)["input_ids"]
            for _rel, lines in blocks
        ]
        out = [0.0] * len(block_ids)
        # Length-bucket the blocks so each batch's [B, L, vocab] logits stay small; bucket by
        # the conditioned length (block + objective prefix), which is the larger of the two.
        order = sorted(range(len(block_ids)), key=lambda i: len(block_ids[i]))
        batch: list[int] = []
        batch_tokens = 0

        def flush() -> None:
            nonlocal batch, batch_tokens
            if not batch:
                return
            bids = [block_ids[i] for i in batch]
            enc = tokenizer.pad({"input_ids": bids}, padding=True, return_tensors="pt")
            ids = enc["input_ids"].to("cuda")
            mask = enc["attention_mask"].to("cuda")
            with torch.inference_mode():
                logits = model(input_ids=ids, attention_mask=mask).logits
            nlp = next_logprobs(logits, ids)  # [B, L-1]: log P(ids[j+1] | ids[<=j])
            del logits
            cond_enc = tokenizer.pad(
                {"input_ids": [q_ids + sep_ids + b for b in bids]},
                padding=True,
                return_tensors="pt",
            )
            cond_ids = cond_enc["input_ids"].to("cuda")
            cond_mask = cond_enc["attention_mask"].to("cuda")
            with torch.inference_mode():
                cond_logits = model(input_ids=cond_ids, attention_mask=cond_mask).logits
            cond_nlp = next_logprobs(cond_logits, cond_ids)
            del cond_logits
            for row, i in enumerate(batch):
                n = len(bids[row])
                uncond = [0.0, *nlp[row, : n - 1].tolist()]  # block token c -> nlp[c-1]
                cond = cond_nlp[row, base - 1 : base - 1 + n].tolist()
                out[i] = sum(cond[c] - uncond[c] for c in range(n)) / n
            batch = []
            batch_tokens = 0

        for i in order:
            n = len(block_ids[i]) + base
            if batch and batch_tokens + n > 2048:
                flush()
            batch.append(i)
            batch_tokens += n
        flush()
        return out

    return score


def pruner_scorer(model_id: str = "ayanami-kitasan/code-pruner") -> BlockScorer:
    """Track B: SWE-Pruner keep-probability averaged over each block (vendor the model)."""
    import json  # lazy: core package never imports torch

    import torch
    import torch.nn as nn
    from huggingface_hub import hf_hub_download
    from safetensors.torch import load_file
    from transformers import AutoConfig, AutoModel, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_id, local_files_only=True)
    config = json.load(open(hf_hub_download(model_id, "config.json", local_files_only=True)))
    backbone_cfg = AutoConfig.from_pretrained(
        config["backbone_model_name_or_path"], local_files_only=True
    )

    class _CRF(nn.Module):  # type: ignore[misc]  # nn is Any (torch stubs absent)
        def __init__(self, num_tags: int = 2) -> None:
            super().__init__()
            self.transitions = nn.Parameter(torch.zeros(num_tags, num_tags))
            self.start_transitions = nn.Parameter(torch.zeros(num_tags))
            self.end_transitions = nn.Parameter(torch.zeros(num_tags))

    class _CompressionHead(nn.Module):  # type: ignore[misc]
        def __init__(self, input_dim: int, bottleneck: int, dropout: float) -> None:
            super().__init__()
            self.feature_extractor = nn.Sequential(
                nn.LayerNorm(input_dim),
                nn.Linear(input_dim, bottleneck, dtype=torch.float32),
                nn.GELU(),
                nn.Dropout(dropout),
                nn.Linear(bottleneck, 2, dtype=torch.float32),
            )
            # CRF transition params are trained but unused at inference (marginal sigmoid).
            self.crf = _CRF()

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            return self.feature_extractor(x)

    class _TokenScorer(nn.Module):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            self.backbone = AutoModel.from_config(backbone_cfg)
            self.backbone = self.backbone.to(dtype=torch.bfloat16)
            hidden = backbone_cfg.hidden_size
            num_layers = backbone_cfg.num_hidden_layers
            self.early = int(num_layers * config["early_layer_ratio"])
            self.middle = int(num_layers * config["middle_layer_ratio"])
            self.fused = hidden * 3
            self.dropout = nn.Dropout(config["dropout"])
            self.fusion_layers = nn.ModuleList(
                [
                    nn.MultiheadAttention(
                        embed_dim=self.fused, num_heads=config["num_heads"], batch_first=True
                    )
                ]
            )
            self.fusion_norms = nn.ModuleList([nn.LayerNorm(self.fused)])
            self.compression_head = _CompressionHead(
                self.fused, config["bottleneck"], config["dropout"]
            )

        def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
            out = self.backbone(
                input_ids=input_ids, attention_mask=attention_mask, output_hidden_states=True
            )
            hs = out.hidden_states
            h = torch.cat([hs[self.early].float(), hs[self.middle].float(), hs[-1].float()], dim=-1)
            mask = (attention_mask == 0).to(h.device)
            for attn, norm in zip(self.fusion_layers, self.fusion_norms, strict=True):
                attn_out, _ = attn(h, h, h, key_padding_mask=mask)
                h = norm(attn_out + h)
            emissions = self.compression_head(self.dropout(h))
            return emissions[:, :, 1] - emissions[:, :, 0]

    model = _TokenScorer().to("cuda")
    model.eval()
    weights_path = hf_hub_download(model_id, "model.safetensors", local_files_only=True)
    state_dict = load_file(weights_path, device="cpu")
    # The released checkpoint wraps the scorer as ``model.*``; load into the bare scorer.
    model.load_state_dict({k.removeprefix("model."): v for k, v in state_dict.items()}, strict=True)

    prefix = (
        "<|im_start|>system\nJudge whether the Document meets the requirements based on the "
        'Query and the Instruct provided. Note that the answer can only be "yes" or "no".'
        "<|im_end|>\n<|im_start|>user\n"
    )
    instruct = "Given a query, judge if the document(code) is related to query."

    def score(objective: str, blocks: Sequence[tuple[str, Sequence[str]]]) -> list[float]:
        query = f"<Instruct>: {instruct}\n<Query>: {objective}\n<Document>: "
        prefix_ids = tokenizer(prefix, add_special_tokens=False)["input_ids"]
        query_ids = tokenizer(query, add_special_tokens=False)["input_ids"]
        head = len(prefix_ids) + len(query_ids)
        code_ids_list = [
            tokenizer("\n".join(lines), add_special_tokens=False)["input_ids"]
            for _rel, lines in blocks
        ]
        out = [0.0] * len(code_ids_list)
        # Length-bucket by conditioned length (prefix + query + code).
        order = sorted(range(len(code_ids_list)), key=lambda i: len(code_ids_list[i]))
        batch: list[int] = []
        batch_tokens = 0

        def flush() -> None:
            nonlocal batch, batch_tokens
            if not batch:
                return
            seqs = [prefix_ids + query_ids + code_ids_list[i] for i in batch]
            enc = tokenizer.pad({"input_ids": seqs}, padding=True, return_tensors="pt")
            ids = enc["input_ids"].to("cuda")
            mask = enc["attention_mask"].to("cuda")
            with torch.inference_mode():
                logits = model(input_ids=ids, attention_mask=mask)
            for row, i in enumerate(batch):
                n = len(code_ids_list[i])
                probs = torch.sigmoid(logits[row, head : head + n].float())
                out[i] = probs.mean().item()
            batch = []
            batch_tokens = 0

        for i in order:
            n = len(code_ids_list[i]) + head
            if batch and batch_tokens + n > 4096:
                flush()
            batch.append(i)
            batch_tokens += n
        flush()
        return out

    return score


def skim_model(
    objective: str,
    files: Sequence[str],
    *,
    root: str | Path,
    budget: int,
    counter: TokenCounter = count_tokens,
    backend: str = "ppl",
    top_k: int = TOP_K,
) -> str:
    """One-shot model skim (loads the model lazily; use :class:`ModelSkimmer` to amortize)."""
    scorer: BlockScorer
    if backend == "ppl":
        scorer = ppl_scorer()
    elif backend == "pruner":
        scorer = pruner_scorer()
    else:
        raise ValueError(f"unknown backend {backend!r}; expected 'ppl' or 'pruner'")
    return ModelSkimmer(root, files, scorer=scorer, counter=counter, top_k=top_k).skim(
        objective, budget=budget
    )
