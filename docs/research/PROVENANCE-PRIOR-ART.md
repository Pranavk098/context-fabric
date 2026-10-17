# Provenance & Receipt Prior Art — Research for "The Seal" (October 2026)

Scope: what exists for provenance/receipts/signing in AI and software supply chains, what LLM
non-determinism actually permits us to claim, what failed and why, who pays for auditability,
and what Context Fabric should build. All factual claims carry a URL. Items we could not verify
are marked [UNVERIFIED]. Written 2026-10-17.

## 1. What provenance/signing/receipt approaches exist

**C2PA / Content Credentials** — the largest media-provenance push: cryptographically signed
manifests (SHA-256, X.509, signatures) attached to content. Adoption is real and growing:
Leica/Samsung/Google Pixel 10 cameras sign at capture; Cloudflare and Fastly preserve
credentials; Adobe ships consumer apps; the C2PA Conformance Program launched mid-2025 with
two security levels (Google Pixel 10 first at the highest level; over 6,000 members/affiliates
by Feb 2026; spec 2.3 adds live-video signing).
Sources: <https://contentauthenticity.org/blog/5000-members-building-momentum-for-a-more-trustworthy-digital-world>,
<https://contentauthenticity.org/blog/raising-the-bar-for-trust-introducing-the-c2pa-conformance-program>,
<https://c2pa.org/news/>, <https://c2pa.ai/news/five-years>.

**Sigstore / SLSA / in-toto** — the working software-supply-chain stack. Sigstore (keyless
signing via Fulcio OIDC certs + Rekor transparency log) is "the de-facto approach to code
signing for open source"; adopted by npm, PyPI, Maven Central, Homebrew, RubyGems, Kubernetes,
Helm; GitHub artifact attestations went GA June 2024; sigstore-go 1.0 (May 2025); OpenSSF
graduated project. SLSA provenance is now built into platforms (GitHub Actions, Packer,
Red Hat Konflux adopting in-toto attestations).
Sources: <https://github.com/ossf/tac/blob/main/process/project-lifecycle-documents/sigstore_graduated_stage.md>,
<https://www.infoq.com/news/2025/08/provenance/>, <https://blog.sigstore.dev/sigstore-go-1-0-now-available/>.
Red Hat ships a production Sigstore deployment (Trusted Artifact Signer) for enterprise SLSA
compliance: <https://developers.redhat.com/products/trusted-artifact-signer>.

**Hugging Face model signing** — the OpenSSF model-signing project 1.0 (April 2025, Google +
NVIDIA + HiddenLayer) signs ML models with Sigstore bundles; integrated into Kaggle and NVIDIA
NGC; verified status shown at upload. Hugging Face's earlier approach, GPG commit signing, was
adopted by fewer than 2% of models.
Sources: <https://openssf.org/blog/2025/04/04/launch-of-model-signing-v1-0-openssf-ai-ml-working-group-secures-the-machine-learning-supply-chain/>,
<https://blog.sigstore.dev/model-transparency-v1.0/>,
<https://openssf.org/blog/2025/07/23/case-study-google-secures-machine-learning-models-with-sigstore/>,
<https://arxiv.org/html/2505.22778> (Blauzvern, "Machine Learning Models Have a Supply Chain
Problem").

**AI gateways / observability audit trails** — Langfuse audit logs, LangSmith audit logs
(tamper-resistant admin-action records, OCSF format, 400-day retention), Portkey enterprise
audit logs. All audit **administrative actions and config changes**, not content provenance;
several are Enterprise-plan-only; traces themselves are mutable app data, not signed receipts.
Sources: <https://langfuse.com/docs/administration/audit-logs>,
<https://docs.langchain.com/langsmith/audit-logs>, <https://docs.portkey.ai/docs/product/enterprise-offering/audit-logs>.

**Model cards / datasheets** — documentation, not cryptography, and weakly adopted (Section 3).

**Agent-era prior art (2025-2026, the closest to "the seal")**:
- *Agent Flight Recorder* (arXiv:2609.01931, Sep 2026, BCCA 2026): canonical serialization +
  hash chaining + Merkle batching + on-chain epoch-root anchoring for agent actions;
  100% detection of edit/delete/reorder/fork tampering; ~48µs/event. <https://arxiv.org/abs/2609.01931>
- *Portable Agent Memory* (arXiv:2605.11032, May 2026): Merkle-DAG provenance with BLAKE3
  content-addressing and Ed25519 root signing for cross-agent memory transfer. <https://arxiv.org/abs/2605.11032>
- *MemMark* (arXiv:2605.25002, 2026): watermarking agent memory state-evolution for
  snapshot-only attribution. <https://arxiv.org/abs/2605.25002>
- *From Agent Traces to Trust* (arXiv:2606.04990): survey/taxonomy of agent execution provenance
  and evidence tracing. <https://arxiv.org/abs/2606.04990>
- CTMS / "Sealed Tool Manifest" (2026): RFC 8785 JCS canonicalization + Sigstore keyless signing
  + in-toto envelope + transparency log for MCP tool metadata — the same canonicalization choice
  as our ADR-0005. <https://github.com/gkanellopoulos/ctms>
- Trustworthy MCP Registry (MDPI *Future Internet*, May 2026): Sigstore + JCS/JWS + RFC 8615,
  ~0.61ms/sign. <https://www.mdpi.com/1999-5903/18/5/243>
- Apple Private Cloud Compute: published, append-only transparency log of production software
  measurements; devices send data only to nodes that attest to logged images. The strongest
  shipping precedent for "verifiable which software ran". <https://security.apple.com/blog/private-cloud-compute/>
- Verifiable inference: zkML is production-adjacent (Lagrange DeepProve open-sourced June 2026,
  self-reported 12M+ proofs; ZKML survey arXiv:2502.18535), ERC-7992 standardizes on-chain
  inference verification; TEE attestation and sampled recomputation (Gensyn, TOPLOC) exist.
  A public tech map classifies all of these as low-to-medium confidence for external
  verification. Sources: <https://lagrange.dev/blog/deepprove-is-now-open-source>,
  <https://eips.ethereum.org/EIPS/eip-7992>, <https://trustbutveri.fyi/>.
- IETF "signed inference receipts" drafts exist (AFiR profile) with self-reported production
  claims — treat as [UNVERIFIED]: <https://datatracker.ietf.org/doc/draft-rotzin-spice-afir-profile/00/>.

**Regulatory demand in force**: EU AI Act Article 12 (automatic logging for high-risk systems)
and Article 19 (retain logs ≥6 months; financial institutions keep them as governance docs):
<https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-12>,
<https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-19>. But the Digital Omnibus
(AI) agreement delayed high-risk obligations to 2 Dec 2027 / 2 Aug 2028, and pushed AI-content
transparency to 2 Dec 2026: <https://www.consilium.europa.eu/en/policies/artificial-intelligence-act/timeline-artificial-intelligence/>.
China's AIGC labeling rules (explicit + implicit metadata marks, mandatory since 1 Sep 2025)
create real cross-border demand: <https://www.reuters.com/world/asia-pacific/chinese-regulators-issue-requirements-labeling-ai-generated-content-2025-03-14/>.

## 2. LLM non-determinism: what is established

- **Temperature 0 does not give determinism.** The dominant cause is *batch-size-dependent
  kernel numerics*: reduction order changes with batch size and with how requests are chunked,
  so a request's output depends on server load. Thinking Machines showed this in Sep 2025 and
  shipped batch-invariant kernels; with them, 1,000 greedy completions were all identical.
  <https://thinkingmachines.ai/blog/defeating-nondeterminism-in-llm-inference/>,
  <https://github.com/thinking-machines-lab/batch_invariant_ops>.
- **Two production engines now ship deterministic modes**: SGLang `--enable-deterministic-inference`
  (built on TML's kernels; compatible with CUDA graphs, chunked prefill, radix cache, temp>0;
  ~34% avg latency overhead) and vLLM `VLLM_BATCH_INVARIANT=1` (documented as **beta**, extra
  GPU-architecture and model coverage limits).
  <https://www.lmsys.org/blog/2025-09-22-sglang-deterministic/>, <https://docs.vllm.ai/en/stable/features/batch_invariance/>.
- **Bitwise output is only guaranteed for a fixed stack.** Batch invariance does not survive
  changes in tensor-parallel size, GPU type, or precision; a 2025 study found greedy decoding
  produced up to 9% accuracy std-dev across 12 runtime configs in BF16 (near-zero in FP32),
  and reasoning-length differences up to ~9,000 tokens. "Same prompt, different answer" across
  machines remains normal. <https://arxiv.org/html/2506.09501>.
- **API providers say so themselves**: OpenAI's `seed` is "best effort... Determinism is not
  guaranteed"; users must diff `system_fingerprint` to detect backend changes; Azure documents
  the same with visible variability even when seed+fingerprint match.
  <https://developers.openai.com/api/docs/guides/advanced-usage>,
  <https://learn.microsoft.com/en-us/azure/foundry-classic/openai/how-to/reproducible-output>.
- **Cache/layout effects are the same phenomenon**: chunked prefill vs KV-cache layout changes
  attention reduction order; SGLang had to make radix-cache prefill produce identical outputs
  to uncached prefill (test mode `radix_cache`, one open checkbox remained as of Feb 2026).
  <https://github.com/sgl-project/sglang/issues/10278>.

**Conclusion:** we can guarantee *context identity* (same bytes in) and, on a pinned stack, we
can *optionally* enable engine-level determinism; we can never guarantee *model output identity*
across machines/configs. Any seal language must say "the notes were identical", never "the
answer was identical".

## 3. What failed, what stalled, and why

- **Hugging Face GPG commit signing**: <2% adoption; PyPI removed PGP signature support for
  similar reasons (unverifiable signatures, low usage). Lesson: optional, manual, key-management
  burden = no adoption. <https://arxiv.org/html/2505.22778>.
- **Model cards**: 60% of HF repos have cards, but only ~14-17% include risk sections, 96% are
  exact duplicates, ethical-mitigation detail appears in <10% of cards, and section prevalence
  *declined* relative to releases. Voluntary documentation without machine verification is
  performative. Sources: <https://doi.org/10.1609/aies.v8i3.36702>,
  <https://dl.acm.org/doi/10.1145/3544548.3581518> (CHI 2023), <https://doi.org/10.1016/j.eswa.2026.132062>.
- **C2PA limits**: metadata is stripped by default in 5 of 6 major social platforms' pipelines;
  LinkedIn strips credentials even while showing a badge; browsers don't surface credentials
  without extensions; RAND criticized a stale threat model (unchanged since v1.0, 2022) and
  overpromising; "absence of credentials" is ambiguous (never marked vs stripped).
  Sources: <https://afip.org/research/metadata-stripping/>,
  <https://www.rand.org/pubs/commentary/2025/06/overpromising-on-digital-provenance-and-security.html>,
  <https://iptc.org/std/guidelines/media-provenance/C2PA-FAQs/IPTC-C2PA-FAQs-v1.0-2027-07-08.pdf>.
- **SLSA**: adoption is real but shallow. A study of 1,523 SLSA-related GitHub issues found the
  top barriers are "complex implementation" and "unclear communication"; Chainguard (a
  practitioner at L3) says customers' reaction to provenance was "meh"; a 61-interview baseline
  found reproducible builds adoption at 0.03. Lessons: complexity kills; L2-for-everyone beats
  L3-for-heroes; provenance nobody reads is shelfware.
  Sources: <https://arxiv.org/html/2409.05014v2>, <https://www.chainguard.dev/unchained/this-shit-is-hard-slsa-l3-and-beyond>,
  <https://csrc.nist.gov/csrc/media/Presentations/2024/adopting-tasks-to-reduce-software-supply-chain-sec/images-media/adopting-tasks-reduce-softwaresc-security-risk.pdf>.
- **Regulation slips too**: the EU AI Act's high-risk logging obligations slipped ~16 months
  (Dec 2027/Aug 2028); do not build a company whose only justification is "the AI Act deadline".
  <https://www.consilium.europa.eu/en/press/press-releases/2026/03/13/council-agrees-position-to-streamline-rules-on-artificial-intelligence/>.
- **What works**: standards with free default paths (Sigstore: keyless, CI-native, automatic) —
  npm/PyPI/Maven/GitHub attestations are used because the platform, not the developer, does the
  work. And hardware-rooted transparency that users benefit from (Apple PCC) survived scrutiny.
  <https://www.infoq.com/news/2025/08/provenance/>, <https://security.apple.com/blog/pcc-security-research/>.

## 4. Who pays for auditability in AI in 2026

- **Enterprises under compliance regimes.** Gartner: AI governance platform spend ~$492M in
  2026, >$1B by 2030; orgs with governance platforms are 3.4x more likely to report high
  governance effectiveness: <https://www.gartner.com/en/newsroom/press-releases/2026-02-17-gartner-global-ai-regulations-fuel-billion-dollar-market-for-ai-governance-platforms>.
- **BFSI first.** SR 26-2 (Apr 2026) replaced SR 11-7; supervisory focus on validation,
  monitoring, and third-party/vendor evidence (non-binding guidance):
  <https://www.fdic.gov/news/press-releases/2026/agencies-issue-revised-model-risk-guidance>,
  <https://www.federalreserve.gov/supervisionreg/srletters/SR2602.htm>.
  Deloitte 2026: only ~1 in 5 companies has a mature governance model for autonomous agents —
  demand is forming faster than supply: <https://www.deloitte.com/us/en/what-we-do/capabilities/applied-artificial-intelligence/content/state-of-ai-in-the-enterprise.html>.
- **Procurement checkboxes.** ISO/IEC 42001 is the emerging vendor gate: ~220 publicly announced
  certifications by Oct 2026, 113 in 2026 alone, 44% enterprise software, OpenAI/Anthropic/
  Google/Microsoft/AWS certified: <https://iso42k.com/state-of-iso-42001>,
  <https://ai.blocksize.hr/blog/en/iso-42001-ai-management-system-certification-2026.html>.
- **Open-source supply chain** pays via package registries (npm/PyPI/Maven) rather than cash.
- **Local vLLM operators**: demand is real but different — reproducibility for debugging and RL
  (vLLM issue: "Inconsistent Output: first call differs from subsequent identical calls with
  temperature=0"; SGLang's determinism work was driven by reproducible RL, not auditors).
  Sources: <https://github.com/vllm-project/vllm/pull/25603>, <https://www.lmsys.org/blog/2025-09-22-sglang-deterministic/>.
  No public evidence that small local operators buy compliance tools today [UNVERIFIED demand].
  The honest wedge: they will pay with attention for a free, one-command verifiable receipt;
  enterprise money is 12-24 months later, tied to ISO 42001/AI Act evidence.

## 5. So what for Context Fabric

Blunt summary: **the idea has strong prior art and real (but delayed) demand; our differential
is the sealed object — the exact compiled context packet — not receipts in general.** Agent
Flight Recorder seals *actions*; CTMS seals *tool metadata*; Portable Agent Memory seals
*transferred memory snapshots*; nobody seals the per-agent compiled packet with a deterministic
compile function, shared CoW prefixes, and negative-knowledge blocks as first-class content.
Our ADR-0003/0005 already give us most of the hard parts (JCS canonical hash with emptied
hashes block, Ed25519, key_id, epoch_number, watermark, kv_prefix_hash).

Ranked recommendations:

1. **Ship the minimum seal now, reusing the packet schema (no new crypto inventions).**
   Per packet: `content_hash` (already RFC 8785 JCS + sha256, signature-independent) +
   Ed25519 signature + `key_id` + `epoch_number` + `prev_epoch_hash`. Chain epochs in an
   append-only local log; a `verify` command recomputes and checks the chain; one test
   proves "flip one byte -> verify fails" and "recompile from same watermark -> identical
   hash on a second machine (deterministic core only, embeddings off)". This is the whole
   minimum: nothing about models, nothing about outputs.
2. **Adopt conservative claims and bake them into the docs/CLI strings.** Claim exactly:
   "proves which memory bytes were handed to the agent and that they are unmodified";
   never "proves why the model answered". Borrow C2PA's own disclaimer — credentials make no
   statement about truth/reliability of the content — and copy it into our wording.
   <https://iptc.org/std/guidelines/media-provenance/C2PA-FAQs/IPTC-C2PA-FAQs-v1.0-2027-07-08.pdf>.
3. **Record inference facts as metadata, not guarantees.** In the receipt, include model id and
   weight fingerprint (e.g., model-signing style hash), tokenizer, sampling params, engine and
   version, `VLLM_BATCH_INVARIANT` flag, `system_fingerprint` (when a hosted API), GPU/precision.
   Say explicitly "output reproduction is out of scope; batch invariance covers batching only,
   not hardware/version changes". <https://docs.vllm.ai/en/stable/features/batch_invariance/>, <https://arxiv.org/html/2506.09501>.
4. **Make the verification story importable to enterprise evidence pipelines.** Emit receipts
   in an in-toto/Sigstore-bundle-compatible envelope (CTMS and OpenSSF model-signing prove this
   is the lingua franca), include OCSF-style event export for SIEMs (LangSmith precedent), and
   map fields to EU AI Act Art. 12/19 record-keeping language (period of use, inputs, persons).
   <https://github.com/gkanellopoulos/ctms>, <https://docs.langchain.com/langsmith/audit-logs>,
   <https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-19>.
5. **Batch receipts into Merkle epochs with inclusion proofs — for cross-agent forks.** The
   Agent Flight Recorder result (Merkle batching, compact proofs, 100% tamper detection at
   ~48µs/event) is directly applicable and cheap; it also lets us prove CoW children share one
   sealed parent prefix hash, which no competitor artifact currently expresses.
   <https://arxiv.org/abs/2609.01931>.
6. **Defer: transparency-log service, on-chain anchoring, zkML/TEE attestation.** Transparency
   logs are operational burden (Yahoo's Rekor-scale lesson: <https://openssf.org/case-studies/2024/02/16/scaling-up-supply-chain-security-implementing-sigstore-for-seamless-container-image-signing/>);
   on-chain anchoring is for parties with no mutual trust (rare in our beachhead); ZK proofs of
   LLM inference are slow and cover small models (<https://lagrange.dev/blog/deepprove-is-now-open-source>).
   Add RFC 3161 timestamps first (cheap, standards-based) if a customer needs third-party time.
7. **Sell it to the beachhead as debugging first, compliance second.** One-command "replay this
   packet" (Tier 0-3 bytes), failed-run forensics, and CI that fails on packet-hash drift is
   immediately useful to vLLM operators who already file nondeterminism issues; the audit
   inspector UI becomes the enterprise upsell already planned in the product brief.
   <https://github.com/vllm-project/vllm/issues/17832>.
8. **Do not overpromise "two machines -> identical sealed notes" without scope qualifiers.**
   From ADR-0003, that holds only with deterministic core, pinned tokenizer, no embeddings (or
   pinned revision), same watermark. Put those four conditions in the receipt and fail the
   comparison with an explicit reason code when violated (embedding_revision_mismatch already
   exists). This turns a risky marketing claim into a precise, testable one.

**Minimum credible version:** signed, chained, replayable packet receipts + `verify` CLI +
tamper/replay tests, with the honest scope statement. **Ultimate version:** per-handoff Merkle
epochs with inclusion proofs and CoW parent linking; in-toto/Sigstore-compatible export;
optional keyless signing via OIDC for enterprise; RFC 3161 timestamps; enterprise console
mapping receipts to AI Act/ISO 42001 evidence; optional engine-level deterministic mode
(`VLLM_BATCH_INVARIANT=1` / SGLang flag) recorded and enabled for teams that opt in.

**What to say about the seal, exactly:** "Context Fabric seals the context: every hand-off
packet is canonically hashed, Ed25519-signed, and chained, so you can prove exactly which notes
an agent received, that they were never altered, and that a recompile from the same watermark
reproduces them byte-for-byte. It does not claim the model's answer is reproducible — model
outputs depend on hardware, batching, and backend versions, as the 2025-2026 determinism
literature shows."
