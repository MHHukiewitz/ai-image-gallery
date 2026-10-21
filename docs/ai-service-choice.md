# Decision: AI service for image analysis

| | |
|---|---|
| **Status** | **Accepted** for the model: Claude Haiku 5.5, Mike's pick on 2026-10-20 from the benchmark in #6 (§8). **Amended** for the service, the same day: production calls Claude through **OpenRouter**, at OpenRouter's default effort, and doesn't move to the Anthropic API. The Anthropic key arrives a week after the project's deadline (§3). |
| **Date** | 2026-10-20. All prices were checked against the vendors' official pages that day, and #6 re-checked every one of them the same day: none had changed. |
| **Deciders** | Mike (owner), orchestrator (research) |
| **Related** | [architecture.md](architecture.md) §2 (D6, D7) and §7 · #6 ([benchmark](../bench/results/2026-10-20/summary.md)) · #31 (Anthropic adapter) · #50 (Jev tag verifier) · #28 (Anthropic key, closed as not planned) |

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
| A | **Claude (Anthropic API):** Haiku 5.5, Sonnet 5.5, Opus 5.5 | ✅ open vocabulary | ✅ | ⚠️ the model can guess hex values, but we compute colors locally (§5) | Per 1M tokens, in / out: Haiku $0.10 / $0.50 · Sonnet $2 / $10 · Opus $4 / $20. Haiku 5.5 charges $0.50 / $2.50 for prompts over 100K tokens, which a downscaled image can't reach. | **Chosen.** See §3. Measured in §8, so far through OpenRouter. |
| B | **Google Gemini API:** 2.5 Flash-Lite, 3.1 Flash-Lite | ✅ | ✅ | ⚠️ same as A | Per 1M tokens: 2.5 Flash-Lite $0.10 / $0.40 · 3.1 Flash-Lite $0.25 / $1.50 | The cheapest and fastest option measured, but its tags and descriptions were clearly the weakest (§8). It's a non-Claude reference in the benchmark. |
| C | **OpenAI API:** gpt-6-luna, the smallest GPT-6 | ✅ (vision confirmed in #6: its model page lists text and image input) | ✅ | ⚠️ same as A | $0.10 / $0.50 per 1M tokens | Comparable on price, a little behind Claude on quality in the benchmark (§8). A second account and SDK would bring no feature gain. |
| D | **Google Cloud Vision:** Label Detection + Image Properties | ✅ fixed taxonomy | ❌ no captioning feature | ✅ | $1.50 per 1k units for each feature. First 1,000 units per feature per month are free. | Rejected. It can't write the sentence, so it would need a second AI service anyway, at a higher combined cost. |
| E | **Amazon Rekognition:** DetectLabels + Image Properties | ✅ fixed taxonomy | ❌ no captions | ✅ RGB values plus pixel share | $1.00 per 1k images (labels) + $0.75 per 1k (properties). Labels have a 1,000 images/month free tier for 12 months; properties have none. | Rejected for the same reason as D. |
| F | **Azure AI Vision Image Analysis 4.0** | ✅ | ✅ captions | ✅ | Not priced: the service is deprecated and retires 2028-09-25 | Rejected. Building new work on a deprecated API is a dead end. |
| G | **Self-hosted vision model** (Ollama, Qwen-VL / Moondream class) on the ops-server VPS | ✅ | ✅ | ⚠️ | $0 per call. It costs CPU and RAM on the shared 8 GB box. | Rejected. With no GPU we expect slow, lower-quality results, and it would compete with the other hosted sites for memory. It's private, though, so it's worth revisiting with a GPU. |
| H | **TypeSafe Jev 1.14** (typed decision model) | ⚠️ closed list only (Choice/Boolean questions) | ❌ typed answers only | ❌ | $0.042 per 1M input tokens, output free. Image token accounting is unpublished. #6 measured about 2,260 input tokens for one photo plus 8–10 tag questions. | **Not usable as the main service:** it can't write the sentence or produce open-vocabulary tags. **Adopted instead as an optional second stage** that checks and merges the main model's tags (#50, see §3). |

## 3. Decision

**We use Claude through OpenRouter, called by the worker with `OpenAICompatAnalyzer`.** The native adapter on the official `anthropic` Python SDK (#31) is built, tested and one config change away. The output is structured, validated with a Pydantic schema, and returns 5–10 tags and one sentence in a single call. Colors are computed locally (§5).

**The model tier: Mike's pick is Claude Haiku 5.5, low effort (2026-10-20).** #6 benchmarked Haiku 5.5, Sonnet 5.5 and Opus 5.5, plus Gemini 2.5 Flash-Lite and GPT-6 Luna as non-Claude references, on 30 images (§8). The Anthropic key wasn't ready, so every model ran **through OpenRouter**. Mike picked from the measured quality, latency and cost. The `ImageAnalyzer` port keeps the choice a config change (`AI_MODEL`, `AI_EFFORT`), not a code change.

**Production setup (Mike, 2026-10-20).** Production runs `anthropic/claude-haiku-5.5` through OpenRouter with `OpenAICompatAnalyzer`, and stays there. The Anthropic key arrives a week after the project's deadline, so #28 is closed as not planned. The adapter sends no effort hint, so OpenRouter's default applies. For this model that's medium, which measured $0.245 per 1k images, against $0.19 at low effort. Both scored the same for tags. The native adapter stays one config change away: `ANALYZER=anthropic`, `ANTHROPIC_API_KEY`, `AI_MODEL=claude-haiku-5-5` and `AI_EFFORT=low`.

Haiku 5.5 at low effort cost $0.19 per 1k images, answered in about 3 s (p50) and returned valid JSON on the first try for all 30 images. On the 10 images rated by eye, its tags scored as well as Sonnet's and Opus's. Why this pick, and what it gives up:
- **Sonnet or Opus would buy no measurable quality here.** The three tiers sit within noise of each other at default effort: tags 4.7–4.8, descriptions 4.6–4.8. Sonnet costs about 16–20 times as much per image ($3.89 per 1k) and Opus about 38–48 times ($7.68–$9.32), with no faster reply. Both still fit in the $200 credit (Sonnet ≈ 51k images a month, Opus ≈ 21k–26k). Choose one only for headroom on harder photos than our set.
- **Gemini 2.5 Flash-Lite is cheaper and faster, but weaker.** It costs 2.4 times less than Haiku and answers about 1.2 s sooner, but it scored 3.5 for tags and 3.4 for descriptions. Its tags are often generic single words, which make text search worse. The saving is $0.11 per 1k images.
- **GPT-6 Luna costs the same as Haiku and scored lower.** It scored 4.4 / 4.4, answered about 1.5 s slower at p50 (it reasons by default), and would add a second account.
- **Low effort is the right setting for Haiku.** Against the default (which OpenRouter reports as medium), it cut cost by 21% and output tokens by half, and the tag score didn't drop (4.7 to 4.8). Its descriptions run long (24 of 30 over the prompt's 25 words), which cost it points on a strict rating but doesn't hurt the product, because the pipeline only caps characters. Tightening the prompt is a better fix than raising the effort.
- **Caveats.** The numbers come from OpenRouter, so latency includes an extra hop and "effort" is OpenRouter's `reasoning.effort` hint. No native run is planned, because production stays on OpenRouter. The quality ratings are one rater's, on 10 images.

### Why: cost
- **On the native adapter, our marginal cost would be $0.** Mike's Max plan includes **$200 a month of Claude API credits** in a linked Console org. Per §4, that covers tens of thousands of images a month even on Opus, and far more on Haiku.
- **Production doesn't use the credit yet.** It runs through OpenRouter at list prices (§3), because the key arrives after the deadline. At $0.245 per 1k images, the demo's volume costs cents. A switch to the native adapter would move the spend onto the credit, with the key in its own spend-limited workspace.
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

**Measured on the benchmark set (2026-10-20, #6).** The verifier checked the tags of three runs over all 30 images: Opus 5.5 at low effort (the best-rated), Haiku 5.5 at low effort (Mike's pick) and Gemini 2.5 Flash-Lite (a weaker tagger, as a stress test). I judged every changed tag by looking at the photo. The repository is public, so the summary keeps the verdicts for the phone photos but not their tags. Details are in the [summary](../bench/results/2026-10-20/summary.md).

| Measurement | Result |
|---|---|
| Reliability | 90 of 90 calls answered. None came near the 5 s production timeout. |
| Latency | p50 345–390 ms, p95 490–600 ms |
| Cost | about 2,260 input tokens per photo: **$0.094 per 1k images**. That's +48% on Haiku at low effort and +1% on Opus. |
| Tags **dropped**, Opus 5.5 (best-rated) | 3 tags in 3 of 30 photos: 1 correct, 1 borderline (`dark sky` on a painting), 1 wrong (a tag scored 0.49, just under the 0.5 cut) |
| Tags **dropped**, Haiku 5.5 (Mike's pick) | 3 tags in 2 photos, all correct (each named something that wasn't in the frame) |
| Tags **dropped**, Gemini 2.5 Flash-Lite (weaker) | 8 tags in 7 photos: 6 correct, 1 borderline, 1 wrong (`globe`, though the figure in the painting stands on one) |
| Tags **merged** | Only 2 overlapping pairs came up in 90 tag sets. Both kept the more specific tag (`oil painting` over `painting`, and a two-word tag over its generic one-word part). |

**What this means:**
- **Drops are mostly right but rare.** On Claude's tags the verifier changed about 1 photo in 10, by 1–2 tags, and 1 of 6 drops was wrong. Claude's tags had few errors for it to catch, so the gain is small.
- **It does catch real mistakes in a weaker tagger's output:** 6 of Gemini's 8 drops named something that wasn't in the frame or was too abstract to see. It also dropped `globe`, which was wrong.
- **Lost specificity didn't recur.** #58's smoke run kept `tree` over `palm tree`. Here both merges kept the specific tag, but 2 cases are too few to rule it out. The Choice question says only "better single search keyword", so adding "prefer the more specific tag" to it (a change in #50's code) is cheap insurance. Turning merges off would also lose almost nothing, since they hardly ever fire.
- **Image input is still labeled experimental by TypeSafe.**

**Recommendation: keep it off in production (`TAG_VERIFIER=none`).** It adds about 0.35 s and $0.094 per 1k images (half of Haiku's own cost) to change about one photo in ten. Revisit it if we ever pick a weaker, cheaper tagger such as Gemini 2.5 Flash-Lite, where it did remove real errors. The code path stays in place and is tested.

## 4. Cost per 1,000 images (estimated, then measured in #6)

**Assumptions behind the estimates:**
- **Input is about 1,450 tokens per image.** A 1024×768 image costs ⌈1024/28⌉ × ⌈768/28⌉ = 1,036 visual tokens under Claude's patch formula, plus about 400 tokens of prompt and schema.
- **Output is 100–400 tokens.** That's the JSON plus a little thinking at low effort.
- **Gemini and OpenAI count image tokens differently.** Their rows apply the same token counts to their list prices so the comparison stays like-for-like. Their real costs may be lower or higher, and #6 measured them.

**How it was measured:** 30 images through each model (§8), through OpenRouter. The cost is the `cost` OpenRouter reports for each request, averaged over the successful calls. For the Claude and Gemini models it equals the token counts at list price, with no markup. OpenRouter's fee for buying credit isn't in it. Where a model has two numbers, they are low effort and the default.

| Option | Estimated $ per 1k images | **Measured $ per 1k images** | Images per $200 credit (estimated) | Images per $200 credit (measured) |
|---|---|---|---|---|
| Claude Haiku 5.5 | $0.20 – $0.35 | **$0.19 – $0.25** | ≈ 570k – 1M | ≈ 0.8M – 1.0M |
| Claude Sonnet 5.5 | $3.90 – $6.90 | **$3.89** (default effort) | ≈ 29k – 51k | ≈ 51k |
| Claude Opus 5.5 | $7.80 – $13.80 | **$7.68 – $9.32** | ≈ 14k – 26k | ≈ 21k – 26k |
| Gemini 2.5 Flash-Lite | $0.19 – $0.31 | **$0.080** | n/a (no credit) | n/a |
| Gemini 3.1 Flash-Lite | $0.51 – $0.96 | not measured | n/a | n/a |
| OpenAI gpt-6-luna | $0.20 – $0.35 | **$0.20** (default effort) | n/a | n/a |
| Cloud Vision (labels + properties) | $3.00, with no sentence | not measured | n/a | n/a |
| Rekognition (labels + properties) | $1.75, with no sentence | not measured | n/a | n/a |
| Self-hosted vision model | $0 per call, plus VPS capacity | not measured | n/a | n/a |

**Where the measurements disagree with the estimates:**
- **Gemini 2.5 Flash-Lite came in 2.4–3.9 times cheaper than estimated.** It billed about 490 input tokens for an image where Claude billed 1,386, so applying Claude's token count to Gemini's price overshot. The estimate for Gemini 3.1 Flash-Lite has the same flaw and probably overshoots too. Nobody has measured it, so don't rely on it.
- **Sonnet and Opus landed at the cheap end or just below their ranges.** Sonnet's $3.89 is just under the $3.90 floor, and Opus at low effort ($7.68) is under its $7.80 floor. The reason is output: they wrote 107–189 tokens, at the low end of the 100–400 assumed.
- **The input assumption held.** Claude billed 1,386 input tokens per image on average, against 1,450 assumed.
- **Luna's reasoning is switched on by default, per OpenRouter's model data.** It billed about 1,000 input and 164 output tokens, so its cost sits at the bottom of its range.

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

**What was run.** `scripts/bench_analyzers.py` sent 30 images through each model, one call at a time, with the worker's own prompt and schema (`PROMPT_VERSION` v1). The images are all 11 religious-art images and 19 of Mike's phone photos, spread evenly over the name-sorted list. `bench/results/2026-10-20/subset.txt` lists the images by file name for the art and as `photo-<hash>` (the start of the content's sha256) for the phone photos, and the images themselves aren't committed. The full tables, the quality rubric and every rating are in **[`bench/results/2026-10-20/summary.md`](../bench/results/2026-10-20/summary.md)**, next to one JSON file per run.

> **Through OpenRouter.** The Anthropic API key wasn't available, so every model, Claude included, was called through OpenRouter's OpenAI-compatible endpoint with `OpenAICompatAnalyzer`. The Claude rows are labeled "via OpenRouter" and are **not yet a check of the native adapter** (#31). OpenRouter bills at the vendors' list prices, so costs carry over. Latency includes OpenRouter's extra hop, and "effort" here is OpenRouter's `reasoning.effort` hint, not Anthropic's own setting. With an Anthropic key, `--models anthropic:claude-haiku-5-5 anthropic:claude-sonnet-5-5 anthropic:claude-opus-5-5 --effort low` would check the native adapter.

| Model | Effort | $ per 1k images | p50 / p95 latency | Valid JSON, first try | Mean tags | Quality: tags / description (1–5) |
|---|---|---|---|---|---|---|
| Claude Haiku 5.5 (via OpenRouter) | default | $0.245 | 3.2 s / 5.8 s | 100% | 8.6 | 4.7 / 4.6 |
| Claude Haiku 5.5 (via OpenRouter) | low (hint) | **$0.194** | 3.1 s / 4.7 s | 100% | 8.8 | 4.8 / 4.2 † |
| Claude Haiku 5.5 (via OpenRouter) | medium (hint) | $0.247 | 3.4 s / 7.2 s | 100% | 8.7 | 4.8 / 4.4 † |
| Claude Sonnet 5.5 (via OpenRouter) | default | $3.893 | 3.8 s / 5.1 s | 100% | 9.8 | 4.8 / 4.7 |
| Claude Opus 5.5 (via OpenRouter) | default | $9.324 | 5.0 s / 10.3 s | 100% | 9.5 | 4.7 / 4.7 |
| Claude Opus 5.5 (via OpenRouter) | low (hint) | $7.683 | 4.5 s / 6.2 s | 100% | 9.6 | 4.8 / 4.8 |
| Gemini 2.5 Flash-Lite (via OpenRouter) | default | $0.080 | 1.8 s / 2.2 s | 100% | 8.2 | 3.5 / 3.4 |
| OpenAI gpt-6-luna (via OpenRouter) | default | $0.202 | 4.5 s / 6.7 s | 100% | 8.6 | 4.4 / 4.4 |

All 240 calls succeeded: 8 runs × 30 images, with no refusals, retries or correction requests. † These descriptions lost points only for running past the prompt's 25-word limit (24 of 30 at low effort). Their content scored 4.7.

**Model ids.** These are the cheapest current ids that serve normal requests: `anthropic/claude-haiku-5.5`, `anthropic/claude-sonnet-5.5`, `anthropic/claude-opus-5.5`, `google/gemini-2.5-flash-lite` and `openai/gpt-6-luna`. Their OpenRouter prices equal the vendors' list prices in §2. The `:batch` variants are half price, but OpenRouter rejects them on the chat endpoint because they're asynchronous, so they can't serve the worker. Luna accepts images, so no OpenAI fallback model was needed.

**How quality was rated.** The agent that ran the benchmark rated 10 images, 5 art and 5 phone photos, by looking at each one. The outputs were shuffled with model names hidden and scored on a fixed rubric (in the summary). With 10 images, 0.2 points is one point on one image. Read the scores as tiers, and Mike can spot-check any row.

**What it shows:**
- **Quality is a tier, and all three Claude tiers are in the top one.** Tags 4.7–4.8, descriptions 4.6–4.8. The few real errors were an invented color on a black-and-white engraving (`red crosses`, from three runs) and one wrongly named material in a phone photo.
- **Cost doesn't track quality.** Opus costs about 38–48 times what Haiku does, and scored the same.
- **Gemini 2.5 Flash-Lite is the cheapest and fastest, and clearly the weakest,** with generic tags and, on the phone photos, descriptions that sometimes invent a detail.
- **Every model returned valid JSON first time,** and every call finished within 11.9 s (the slowest was an Opus call), well inside the worker's 30 s timeout.
- **Effort.** OpenRouter's default for these Claude models is medium: Haiku at medium and at default match in cost and tokens. Low effort cut Haiku's output tokens by half and its cost by 21%, and Opus's cost by 18%.
- **Harness note.** The benchmark also found that `normalize_description` cuts a description after "St." (two St Augustine descriptions were saved ending in "St."). That is a bug in `domain/tags.py`, outside this ticket, and it needs its owner's fix.

**Spend:** $0.656 of a $0.75 cap on the OpenRouter key, and $0.009 for the Jev runs, which TypeSafe bills. The Jev measurements are in §3.

**Mike's pick: Claude Haiku 5.5, low effort (2026-10-20).** The reasons and trade-offs are in §3. Production runs `anthropic/claude-haiku-5.5` through OpenRouter at default effort, which measured $0.245 per 1k images, and stays there (§3).

**Privacy.** The repository is public, so the phone photos appear only as `photo-<hash>`, and the tags and descriptions the models wrote for them are withheld from the results files and from this document. Their latency, tokens, cost, counts and quality scores are kept. `bench_analyzers.py --redact` does this. The art outputs are kept as they are.

## Sources (checked 2026-10-20)

- Anthropic: [pricing](https://platform.claude.com/docs/en/about-claude/pricing) · [vision, including token formula and data handling](https://platform.claude.com/docs/en/build-with-claude/vision) · [API credits for Max and Team plans](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers)
- Google: [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) (page updated 2026-10-19) · [Cloud Vision pricing](https://cloud.google.com/vision/pricing)
- OpenAI: [API pricing](https://developers.openai.com/api/docs/pricing) · [gpt-6-luna model page, which lists image input](https://developers.openai.com/api/docs/models/gpt-6-luna)
- AWS: [Rekognition pricing](https://aws.amazon.com/rekognition/pricing/) · [DetectLabels image properties](https://docs.aws.amazon.com/rekognition/latest/APIReference/API_DetectLabelsImageProperties.html)
- Microsoft: [Image Analysis overview and retirement notice](https://learn.microsoft.com/en-us/azure/ai-services/computer-vision/overview-image-analysis)
- TypeSafe Jev: [Vercel changelog](https://vercel.com/changelog/typesafe-ais-jev-1-14-is-now-available-on-ai-gateway) · [image input guide, which gives the $0.042 per 1M input tokens and the experimental label](https://smartscope.blog/en/blog/jev-image-input-guide-asset-handling-choice-box-2026/)
- OpenRouter (benchmark routing): [model list with prices and modalities](https://openrouter.ai/api/v1/models), read 2026-10-20
