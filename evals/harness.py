"""Telemetry harness — provider-agnostic OpenAI-compatible client + metrics.

Designed for the Stage B falsification spike: it measures the numbers that decide the
project (cached vs. dynamic input tokens, prefix adjacency, TTFT, and thus VRAM/prefill
proxy). It talks to any OpenAI-compatible endpoint (vLLM, LMCache-fronted vLLM, Ollama,
or a cloud API) so the spike is not coupled to one runtime.

No routing/scoring logic here — this is measurement only.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
import tiktoken

_ENCODER = tiktoken.get_encoding("o200k_base")


def count_tokens(text: str) -> int:
    return len(_ENCODER.encode(text))


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _parse_vllm_cache(text: str) -> tuple[float, float]:
    """Return (prefix_cache_hits_total, prefix_cache_queries_total) from a Prometheus dump."""
    hits = queries = 0.0
    for line in text.splitlines():
        if line.startswith("vllm:prefix_cache_hits_total"):
            hits = float(line.rsplit(" ", 1)[1])
        elif line.startswith("vllm:prefix_cache_queries_total"):
            queries = float(line.rsplit(" ", 1)[1])
    return hits, queries


class MetricsScraper:
    """Scrapes vLLM's Prometheus endpoint for authoritative prefix-cache counters.

    vLLM does NOT populate ``usage.prompt_tokens_details.cached_tokens`` (verified on
    0.31.0), so the OpenAI ``usage`` field under-reports cache hits by 100%. The
    ``/metrics`` counters are the source of truth for local-serving measurement.
    """

    def __init__(self, metrics_url: str) -> None:
        self.metrics_url = metrics_url

    def cache(self) -> tuple[float, float]:
        try:
            r = httpx.get(self.metrics_url, timeout=5.0)
            if r.status_code == 200:
                return _parse_vllm_cache(r.text)
        except Exception:
            pass
        return (0.0, 0.0)


@dataclass
class TurnMetrics:
    turn: int
    prompt_tokens: int
    cached_tokens: int
    dynamic_tokens: int
    completion_tokens: int
    prefix_hash: str
    prefix_continuity: bool  # previous prompt is an exact prefix of this one
    ttft_ms: float
    latency_ms: float
    text: str = ""


@dataclass
class RunMetrics:
    label: str
    model: str
    turns: list[TurnMetrics] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        n = len(self.turns)
        total_prompt = sum(t.prompt_tokens for t in self.turns)
        total_cached = sum(t.cached_tokens for t in self.turns)
        total_dynamic = sum(t.dynamic_tokens for t in self.turns)
        continuity = sum(1 for t in self.turns if t.prefix_continuity)
        return {
            "label": self.label,
            "model": self.model,
            "turns": n,
            "prompt_tokens_total": total_prompt,
            "cached_tokens_total": total_cached,
            "dynamic_tokens_total": total_dynamic,
            "prefix_cache_hit_ratio": (total_cached / total_prompt) if total_prompt else 0.0,
            "prefix_continuity_ratio": (continuity / n) if n else 0.0,
            "mean_ttft_ms": (sum(t.ttft_ms for t in self.turns) / n) if n else 0.0,
            "mean_latency_ms": (sum(t.latency_ms for t in self.turns) / n) if n else 0.0,
            "turns_detail": [asdict(t) for t in self.turns],
        }


class OpenAICompatibleClient:
    """Minimal streaming chat client for any OpenAI-compatible endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "EMPTY",
        timeout_s: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(timeout=timeout_s)

    def _usage_cached(self, usage: dict[str, Any]) -> int:
        details = usage.get("prompt_tokens_details") or {}
        return int(details.get("cached_tokens", 0) or 0)

    def chat(self, messages: list[dict[str, str]], max_tokens: int = 256) -> dict[str, Any]:
        """Streaming chat completion; returns text + usage + timings."""
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"{self.base_url}/chat/completions"

        text_parts: list[str] = []
        usage: dict[str, Any] = {}
        ttft_ms: float | None = None
        start = time.perf_counter()
        with self._client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code >= 400:
                body = resp.read().decode("utf-8", "replace")
                raise httpx.HTTPStatusError(
                    f"{resp.status_code}: {body}", request=resp.request, response=resp
                )
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta", {}).get("content")
                    if delta:
                        if ttft_ms is None:
                            ttft_ms = (time.perf_counter() - start) * 1000
                        text_parts.append(delta)
        latency_ms = (time.perf_counter() - start) * 1000
        return {
            "text": "".join(text_parts),
            "prompt_tokens": int(usage.get("prompt_tokens", 0)),
            "completion_tokens": int(usage.get("completion_tokens", 0)),
            "cached_tokens": self._usage_cached(usage),
            "ttft_ms": ttft_ms if ttft_ms is not None else latency_ms,
            "latency_ms": latency_ms,
        }

    def close(self) -> None:
        self._client.close()


class Harness:
    """Runs a sequence of message-states, recording per-turn cache/token metrics."""

    def __init__(
        self,
        client: OpenAICompatibleClient,
        label: str,
        scraper: MetricsScraper | None = None,
    ) -> None:
        self.client = client
        self.metrics = RunMetrics(label=label, model=client.model)
        self._prev_prompt: str | None = None
        self._scraper = scraper

    def turn(self, messages: list[dict[str, str]], max_tokens: int = 256) -> str:
        prompt = "\n".join(m["content"] for m in messages)
        before = self._scraper.cache() if self._scraper else (0.0, 0.0)
        result = self.client.chat(messages, max_tokens=max_tokens)
        after = self._scraper.cache() if self._scraper else (0.0, 0.0)
        continuity = self._prev_prompt is not None and prompt.startswith(self._prev_prompt)
        self._prev_prompt = prompt
        prompt_tokens = result["prompt_tokens"]
        # Prefer the provider usage field when present; otherwise use the server-side
        # prefix-cache counter delta (vLLM does not fill prompt_tokens_details).
        cached = int(result["cached_tokens"])
        if cached == 0 and self._scraper is not None:
            cached = max(0, int(after[0] - before[0]))
        self.metrics.turns.append(
            TurnMetrics(
                turn=len(self.metrics.turns) + 1,
                prompt_tokens=prompt_tokens,
                cached_tokens=cached,
                dynamic_tokens=max(prompt_tokens - cached, 0),
                completion_tokens=result["completion_tokens"],
                prefix_hash=prompt_hash(prompt),
                prefix_continuity=continuity,
                ttft_ms=result["ttft_ms"],
                latency_ms=result["latency_ms"],
                text=result["text"],
            )
        )
        return result["text"] if isinstance(result["text"], str) else str(result["text"])
