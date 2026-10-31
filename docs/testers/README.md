# Context Fabric Tester Guides

1. **[`checkup.md`](checkup.md):** Run `uv run cfxd checkup` first for the combined numbers-only summary (diet, fidelity, sub-agents).
2. **[`diet.md`](diet.md):** Inspect starting-prefix token estimates and unused skills or MCP connectors (`uv run cfxd diet --since 30d --share`).
3. **[`fidelity.md`](fidelity.md):** Measure `/compact` path retention and dropped edits (`uv run cfxd fidelity --since 30d`; no `--share` flag).
4. **[`brief-opt-in.md`](brief-opt-in.md):** Optional, experimental sub-agent handoff brief test (`uv run cfxd handoff-stats --share`, `uv run cfxd brief --share`).

- **Report form:** Submit findings via [`.github/ISSUE_TEMPLATE/tester-feedback.yml`](../../.github/ISSUE_TEMPLATE/tester-feedback.yml) — **numbers only, never prompts, paths or commands**.
- **Honesty rules:** No savings claims; E-1 found re-discovery is a small share of total session cost on the maintainer's machine ("1% to 3%, one machine"), so Brief is an opt-in experimental test, not a promise.
- **Retired features:** `fold`, `bound`, and `checkpoint` are retired and are not part of testing.
