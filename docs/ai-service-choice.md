# Decision: AI service for image analysis

| | |
|---|---|
| **Status** | **Accepted** for the service: Claude through the Anthropic API. **Open** for the model tier: Mike picks it from the measured benchmark in #6. |
| **Date** | 2026-10-20. All prices were checked against the vendors' official pages that day. |
| **Deciders** | Mike (owner), orchestrator (research) |
| **Related** | [architecture.md](architecture.md) §2 (D6, D7) and §7 · #6 (benchmark) · #31 (Anthropic adapter) · #50 (Jev tag verifier) · #28 (API key) |

## 1. What the challenge needs

Per image, in the background:

| Requirement | What it needs from a service |
|---|---|
| 5–10 relevant tags | Open-vocabulary labels: words the model chooses itself, not picks from a fixed list. They drive the text search, so recall matters more than taxonomy. |
| One descriptive sentence | Natural-language generation. **This rules out pure label APIs on their own.** |
| Top 3 dominant colors | Exact hex values. Color search depends on them, so precision matters. |
| Async, failure-tolerant | Predictable latency and clear retryable vs. permanent errors. |

**Criteria, in priority order:**
1. Covers tags *and* sentence in **one call**.
2. Cost per 1,000 images.
3. Reliable structured output (valid JSON every time).
4. Integration effort and SDK quality.
5. Data handling for users' private photos.
6. Latency.

## 2. Options considered

| # | Option | Tags | Sentence | Colors | List price (as of 2026-10-20) | Verdict |
|---|---|---|---|---|---|---|
| A | **Claude (Anthropic API):** Haiku 5.5, Sonnet 5.5, Opus 5.5 | ✅ open vocabulary | ✅ | ⚠️ the model can guess hex values, but we compute colors locally (§5) | Per 1M tokens, in / out: Haiku $0.10 / $0.50 · Sonnet $2 / $10 · Opus $4 / $20 | **Chosen.** See §3. |
| B | **Google Gemini API:** 2.5 Flash-Lite, 3.1 Flash-Lite | ✅ | ✅ | ⚠️ same as A | Per 1M tokens: 2.5 Flash-Lite $0.10 / $0.40 · 3.1 Flash-Lite $0.25 / $1.50 | Strong runner-up, about as cheap as Haiku. It's the non-Claude reference in the benchmark. |
| C | **OpenAI API:** gpt-6-luna, the smallest GPT-6 | ✅ (vision support to be confirmed in #6) | ✅ | ⚠️ same as A | $0.10 / $0.50 per 1M tokens | Comparable on price. A second account and SDK would bring no feature gain. |
| D | **Google Cloud Vision:** Label Detection + Image Properties | ✅ fixed taxonomy | ❌ no captioning feature | ✅ | $1.50 per 1k units for each feature. First 1,000 units per feature per month are free. | Rejected. It can't write the sentence, so it would need a second AI service anyway, at a higher combined cost. |
| E | **Amazon Rekognition:** DetectLabels + Image Properties | ✅ fixed taxonomy | ❌ no captions | ✅ RGB values plus pixel share | $1.00 per 1k images (labels) + $0.75 per 1k (properties). Labels have a 1,000 images/month free tier for 12 months; properties have none. | Rejected for the same reason as D. |
| F | **Azure AI Vision Image Analysis 4.0** | ✅ | ✅ captions | ✅ | Not priced: the service is deprecated and retires 2028-09-25 | Rejected. Building new work on a deprecated API is a dead end. |
| G | **Self-hosted vision model** (Ollama, Qwen-VL / Moondream class) on the ops-server VPS | ✅ | ✅ | ⚠️ | $0 per call. It costs CPU and RAM on the shared 8 GB box. | Rejected. With no GPU we expect slow, lower-quality results, and it would compete with the other hosted sites for memory. It's private, though, so it's worth revisiting with a GPU. |
| H | **TypeSafe Jev 1.14** (typed decision model) | ⚠️ closed list only (Choice/Boolean questions) | ❌ typed answers only | ❌ | $0.042 per 1M input tokens, output free. Image token accounting is unpublished. | **Not usable as the main service:** it can't write the sentence or produce open-vocabulary tags. **Adopted instead as an optional second stage** that checks and merges the main model's tags (#50, see §3). |

## 3. Decision

**We use Claude through the Anthropic API, called by the worker through the official `anthropic` Python SDK (#31).** The output is structured, validated with a Pydantic schema, and returns 5–10 tags and one sentence in a single call. Colors are computed locally (§5).

**The model tier is still open.** #6 benchmarks Haiku 5.5, Sonnet 5.5 and Opus 5.5 at low effort, plus a non-Claude reference (B). Mike picks from the measured quality, latency and cost. The `ImageAnalyzer` port makes the choice a config change (`AI_MODEL`, `AI_EFFORT`), not a code change.

### Why: cost
- **Our marginal cost is $0.** Mike's Max plan includes **$200 a month of Claude API credits** in a linked Console org. Per §4, that covers tens of thousands of images a month even on Opus, and far more on Haiku.
- **A spend-limited workspace contains mistakes.** The API key lives in its own workspace (#28), so a bug can't drain the credit.
- **Without the credit, the price race is close.** Haiku 5.5, gpt-6-luna and Gemini 2.5 Flash-Lite all cost a few tenths of a dollar per 1k images, so cost alone wouldn't pick between them. The credit settles it.
- **The label APIs cost more and do less.** Options D and E cost $1.75–3.00 per 1k images and still need an LLM for the sentence.

### Why: features
- **One call returns everything we need.** It produces open-vocabulary tags (better for free-text search than a fixed taxonomy) and a sentence.
- **Structured outputs come back as validated objects.** `messages.parse` checks the response against our schema, so the JSON-repair path rarely fires.
- **Effort is tunable per request.** Tagging is simple, so we start at `low`.
- **Refusals are explicit.** They arrive as a `refusal` stop reason that we map to a permanent, user-visible "declined" state. Sonnet and Opus also support server-side fallbacks.

### Why: ease of use
- **We already have the account and the tooling.** The account exists, the credits exist, and the swarm already works with Claude tooling and docs.
- **The SDK covers the plumbing.** It ships typed exceptions (rate limit, 5xx, auth), built-in retries and async support, so the adapter is small (#31).
- **Image prep is simple, with predictable cost.** We send one base64 image block, downscaled by us to a 1024 px long side.

### Why: data handling
- **Anthropic doesn't train on uploaded images.** Images also aren't stored beyond the request ([vision docs](https://platform.claude.com/docs/en/build-with-claude/vision)). Users' private photos leave our system only for the duration of one API call.

### Optional second stage: tag verification with Jev (#50)

Jev can't *generate* tags, but it can *judge* them cheaply. With `TAG_VERIFIER=jev`, the worker sends the image and the main model's tags to Jev in one call:
- one yes/no question per tag ("does this tag describe something visible?");
- up to 3 merge questions for near-duplicates.

It drops tags scored below 0.5 and merges duplicates, but always keeps 5–10 tags. **It's best-effort:** if Jev fails or times out, the original tags are kept and the job still completes.

**Spike (2026-10-20, `jev-1.14.0`, one image, one call with 13 questions):**

| Measurement | Result |
|---|---|
| 9 real tags (house, sun, hill, …) | probability 0.97–0.98 |
| 3 decoys (cat, beach, car) | probability 0.02–0.04 |
| Merge `house` vs `red house` | kept `red house` (0.74) |
| Latency | 597 ms |
| Cost | 2,764 input tokens, output free: about $0.0001 per image, or $0.12 per 1k |

**Trade-off:** image input is still labeled experimental by TypeSafe, and the spike used an easy cartoon. So the stage is **off by default**, and #6 measures it on the 20 real photos before we decide whether production enables it.

## 4. Cost per 1,000 images (estimate, until #6 measures it)

**Assumptions:**
- **Input is about 1,450 tokens per image.** A 1024×768 image costs ⌈1024/28⌉ × ⌈768/28⌉ = 1,036 visual tokens under Claude's patch formula, plus about 400 tokens of prompt and schema.
- **Output is 100–400 tokens.** That's the JSON plus a little thinking at low effort.
- **Gemini and OpenAI count image tokens differently.** Their rows apply the same token counts to their list prices so the comparison stays like-for-like. Their real costs may be lower or higher, and #6 measures them.

| Option | Estimated $ per 1k images | Images per $200 credit |
|---|---|---|
| Claude Haiku 5.5 | $0.20 – $0.35 | ≈ 570k – 1M |
| Claude Sonnet 5.5 | $3.90 – $6.90 | ≈ 29k – 51k |
| Claude Opus 5.5 | $7.80 – $13.80 | ≈ 14k – 26k |
| Gemini 2.5 Flash-Lite | $0.19 – $0.31 | n/a (no credit) |
| Gemini 3.1 Flash-Lite | $0.51 – $0.96 | n/a |
| OpenAI gpt-6-luna | $0.20 – $0.35 | n/a |
| Cloud Vision (labels + properties) | $3.00, with no sentence | n/a |
| Rekognition (labels + properties) | $1.75, with no sentence | n/a |
| Self-hosted vision model | $0 per call, plus VPS capacity | n/a |

**Cost controls built into the design:**
- **Downscale before sending:** the 1024 px long side caps visual tokens at about 1,400 per image.
- **Cache by content hash:** the same image with the same model, effort and prompt version never gets billed twice (architecture D9).
- **Compute colors locally,** so they cost no tokens.

## 5. Dominant colors: computed locally, not by the AI

The worker extracts the top 3 colors with Pillow:
1. downsample to 128 px;
2. median-cut quantize to 8 colors;
3. rank by pixel count;
4. drop near-duplicates, meaning colors with ΔE76 < 10 in CIE Lab.

| | Local extraction (chosen) | Asking the AI | Label APIs (D, E) |
|---|---|---|---|
| Cost | $0 | output tokens on every call | $0.75–1.50 per 1k |
| Accuracy | exact pixel statistics | guessed hex values | good |
| Deterministic | yes | no | yes |
| Works when the AI fails | **yes**, so color search keeps working | no | separate call |

## 6. How each requirement is met

| Requirement | Implementation |
|---|---|
| 5–10 tags | The schema enforces 5–10 tags, which are then normalized (lowercase, deduplicated, clamped). Fewer than 5 triggers one repair attempt (#9, #31). |
| One sentence | A schema field, normalized to the first sentence and at most 300 characters. |
| Top 3 colors | Local Pillow extractor (#10), stored as `#rrggbb` and converted to Lab for color search (#5). |
| Async in the background | Upload returns immediately with `status = pending`. The worker claims jobs from a Postgres queue with `SKIP LOCKED` (#4, #11), and the UI polls until the status is `done` (architecture §4). |
| Graceful AI failures | Rate limits, 5xx and timeouts are retried with backoff (up to 3 attempts per job). Auth errors, bad requests and refusals fail permanently with a message. Colors are kept either way, and a Retry button calls reanalyze (architecture §4). |

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| The Max credit runs out, or doesn't roll over | The workspace spend limit caps it. If it hits zero, jobs fail as retryable, then as `failed` with "AI service unavailable", and recover with Retry. Switching to a cheaper tier is one `AI_MODEL` change. |
| Model deprecation | The model ID is config, not code, and the prompt version is part of the cache key. |
| Rate limits or a provider outage | SDK retries, then job-level backoff, then a visible failed state. The OpenAI-compatible adapter (#9) can route to Gemini by config. |
| Vendor lock-in | The `ImageAnalyzer` port and a second adapter are already in the codebase. The benchmark keeps the comparison measured, not assumed. |
| Refusals on benign photos | They're mapped to a permanent "declined" state and colors still work. Sonnet and Opus get server-side fallbacks. |

## 8. Benchmark results

> **Pending #6.** #6 runs `scripts/bench_analyzers.py` over 20 varied photos (#28) and fills this section:
> - one table: cost per 1k images, p50/p95 latency, valid-JSON %, mean tag count and a 1–5 quality score per model;
> - a link to `bench/results/<date>/summary.md`;
> - **Mike's model pick and its rationale.**

## Sources (checked 2026-10-20)

- Anthropic: [pricing](https://platform.claude.com/docs/en/about-claude/pricing) · [vision, including token formula and data handling](https://platform.claude.com/docs/en/build-with-claude/vision) · [API credits for Max and Team plans](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers)
- Google: [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) (page updated 2026-10-19) · [Cloud Vision pricing](https://cloud.google.com/vision/pricing)
- OpenAI: [API pricing](https://developers.openai.com/api/docs/pricing)
- AWS: [Rekognition pricing](https://aws.amazon.com/rekognition/pricing/) · [DetectLabels image properties](https://docs.aws.amazon.com/rekognition/latest/APIReference/API_DetectLabelsImageProperties.html)
- Microsoft: [Image Analysis overview and retirement notice](https://learn.microsoft.com/en-us/azure/ai-services/computer-vision/overview-image-analysis)
- TypeSafe Jev: [Vercel changelog](https://vercel.com/changelog/typesafe-ais-jev-1-14-is-now-available-on-ai-gateway) · [image input guide](https://smartscope.blog/en/blog/jev-image-input-guide-asset-handling-choice-box-2026/)
