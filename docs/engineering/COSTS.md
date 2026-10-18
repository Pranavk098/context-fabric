# Cost model (`cfx demo --price`)

`cfx demo --price <preset|custom:...>` attaches a `cost` section to the receipt and prints
per-arm dollars. It is a **token-count × published list price** model: it estimates what the
tokens in the receipt would cost at a public per-token price. It is not a bill, and it does
not model provider billing quirks.

## Formula

For one call or arm, with rates in USD per million tokens (`M`):

```
input_usd        = (prompt_tokens − cached_tokens) × input_per_mtok / 1e6
cached_input_usd = cached_tokens × cached_input_per_mtok / 1e6
cache_write_usd  = cache_writes × cache_write_per_mtok / 1e6
output_usd       = completion_tokens × output_per_mtok / 1e6
total_usd        = input_usd + cached_input_usd + cache_write_usd + output_usd
```

- `cached_tokens` is clamped to `[0, prompt_tokens]`; negative token counts are rejected.
- When a preset has no cache-write rate, cache writes are billed at the plain input rate.
- `cached_tokens = 0` and `cache_writes = 0` reduce the formula to input + output.

## Presets

Snapshot of public list prices, **checked 2026-10-18**. Providers change prices; verify
before relying on a number.

| preset | input $/M | output $/M | cached input $/M | cache write $/M | source |
|--------|----------:|-----------:|-----------------:|----------------:|--------|
| `openai:gpt-4o` | 2.50 | 10.00 | 1.25 | — | https://platform.openai.com/docs/pricing |
| `openai:gpt-4o-mini` | 0.15 | 0.60 | 0.075 | — | https://platform.openai.com/docs/pricing |
| `anthropic:sonnet` | 3.00 | 15.00 | 0.30 | 3.75 | https://docs.anthropic.com/en/docs/about-claude/pricing |
| `anthropic:haiku` | 1.00 | 5.00 | 0.10 | 1.25 | https://docs.anthropic.com/en/docs/about-claude/pricing |
| `local:free` | 0 | 0 | 0 | — | n/a (self-hosted; no per-token price) |

The single source of truth is `PRESETS` in `sidecar/costs.py` (each entry also stores its
source URL and `checked` date). To add a preset, append an entry there; the CLI accepts any
key automatically.

## Overrides

- `--price custom:IN,OUT,CACHED[,CACHE_WRITE]` — custom rates in USD per million tokens,
  e.g. `--price "custom:2.5,10,1.25"` or `--price "custom:2.5,10,1.25,3.75"`. Rates must be
  finite and non-negative; the wrong number of values, a non-numeric rate, or unknown
  preset is a CLI error (exit 2). A custom spec is labeled `custom` in the receipt.
- `CF_PRICE_INPUT_PER_MTOK` is the **legacy** env var. It is unchanged: `cfx demo` adds
  `estimated_input_cost_usd` to each arm summary (and `cfx serve` exposes the same field in
  `/fabric/stats`) using the prompt tokens and this single input rate. It does not price
  output, cached tokens, or cache writes. It is independent of `--price`; if both are set,
  both appear. Prefer `--price` for total dollars.

## How receipts compute it

For every arm in the receipt:

1. Take `prompt_tokens_total` and `completion_tokens_total` from the upstream `usage` blocks
   (already in the arm summary).
2. Estimate `cached_tokens` from the vLLM counters when available:
   `round(prompt_tokens_total × clamp(prefix_cache_hits_delta / prefix_cache_queries_delta,
   0, 1))`. This is a **request-level** hit ratio applied proportionally to tokens — vLLM's
   counters count requests, not tokens. If the upstream has no `/metrics`, the deltas are
   null, or queries are 0, cached tokens are 0 and all input is billed at full price.
3. `cache_writes` is not measured by the demo and is always 0.
4. Emit per-arm `total_usd`, `per_request_usd` (`total_usd / requests`), token counts, the
   cached estimate, and component dollars; round to 8 decimal places in JSON.
5. Fleet and repo scenarios: `savings_pct = round((baseline_total − compile_total) /
   baseline_total × 100, 1)`, or `null` when the baseline is free. The baseline is the
   broadcast arm for fleet and the raw arm for repo (`--repo`); negative scenarios have
   control/treated arms and no savings figure.

The assumptions list in the `cost` object records the preset/source, which cached-token
treatment was used, and the model limits below. Mock mode has no `/metrics`, so mock
receipts always use full-price input; their prompt tokens are real for the messages sent,
but completion tokens are a fixed 8.

## Limits

- List prices are a hand-maintained snapshot (checked 2026-10-18); they drift. No automatic
  refresh.
- Provider billing quirks are not modeled: Anthropic cache TTLs (5-minute vs 1-hour write
  premiums), cache minimums, batch discounts, committed-use discounts, tiered pricing,
  taxes, and rounding on the provider side.
- Cached-token counts are an estimate from request-level counters, not token accounting.
  Cache writes are not measured (always 0), so Anthropic write premiums are understated.
- Costs are per arm, not per record; per-record costs are a linear share only if requested
  externally.
- The demo's completion token counts come from upstream `usage`; tokenizer differences
  between `count_tokens` (o200k) and the served model can make token counts approximate
  outside the demo's own fixture sizing.
- `local:free` prices self-hosted inference at zero; GPU/electricity costs are not modeled.
- Mock-mode dollar figures are for UX/wiring only — never cite them as measurements.
