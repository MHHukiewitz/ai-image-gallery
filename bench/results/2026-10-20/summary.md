# Analyzer benchmark, 2026-10-20

30 images from `subset.txt`: 11 public religious-art images and 19 phone photos spread evenly over the sorted list. The images are not committed. The repository is public, so the photos appear only as `photo-<hash>`, and the tags and descriptions the models wrote for them are withheld. Their latency, tokens, cost and counts are kept.

| Model | Effort | $ / 1k images | p50 ms | p95 ms | Success | Valid JSON, 1st try | Mean tags | Tokens in / out | Quality: tags / description |
|---|---|---|---|---|---|---|---|---|---|
| Gemini 2.5 Flash-Lite (via OpenRouter) | default | $0.080 | 1,841 | 2,195 | 30/30 | 100% | 8.2 | 491 / 77 | 3.5 / 3.4 (n=10) |
| GPT-6 Luna (via OpenRouter) | default | $0.202 | 4,520 | 6,692 | 30/30 | 100% | 8.6 | 1,007 / 164 | 4.4 / 4.4 (n=10) |
| Claude Haiku 5.5 (via OpenRouter) | default | $0.245 | 3,206 | 5,832 | 30/30 | 100% | 8.6 | 1,386 / 214 | 4.7 / 4.6 (n=10) |
| Claude Sonnet 5.5 (via OpenRouter) | default | $3.893 | 3,763 | 5,056 | 30/30 | 100% | 9.8 | 1,386 / 112 | 4.8 / 4.7 (n=10) |
| Claude Opus 5.5 (via OpenRouter) | default | $9.324 | 4,979 | 10,264 | 30/30 | 100% | 9.5 | 1,386 / 189 | 4.7 / 4.7 (n=10) |
| Claude Haiku 5.5 (via OpenRouter) | low (hint) | $0.194 | 3,051 | 4,740 | 30/30 | 100% | 8.8 | 1,386 / 110 | 4.8 / 4.2 (n=10) |
| Claude Haiku 5.5 (via OpenRouter) | medium (hint) | $0.247 | 3,371 | 7,157 | 30/30 | 100% | 8.7 | 1,386 / 217 | 4.8 / 4.4 (n=10) |
| Claude Opus 5.5 (via OpenRouter) | low (hint) | $7.683 | 4,544 | 6,201 | 30/30 | 100% | 9.6 | 1,386 / 107 | 4.8 / 4.8 (n=10) |

**How to read it.** Cost is the mean `cost_usd` of the successful calls times 1,000. For OpenRouter models, `cost_usd` is the cost OpenRouter reports for the request; for native Anthropic calls it is computed from token counts at list price. Latency is wall-clock time of one `analyze()` call (image re-encoding, any retry and any correction included), measured one call at a time. Valid JSON counts replies that needed no correction request. Effort *(hint)* is OpenRouter's `reasoning.effort`, sent through the OpenAI-compatible adapter; *default* sends no reasoning setting.

| Model | Effort | Mean description words | Over the prompt's 25-word limit |
|---|---|---|---|
| Gemini 2.5 Flash-Lite (via OpenRouter) | default | 17.1 | 0 of 30 |
| GPT-6 Luna (via OpenRouter) | default | 19.1 | 0 of 30 |
| Claude Haiku 5.5 (via OpenRouter) | default | 25.9 | 17 of 30 |
| Claude Sonnet 5.5 (via OpenRouter) | default | 25.4 | 15 of 30 |
| Claude Opus 5.5 (via OpenRouter) | default | 23.7 | 3 of 30 |
| Claude Haiku 5.5 (via OpenRouter) | low | 27.3 | 24 of 30 |
| Claude Haiku 5.5 (via OpenRouter) | medium | 26.3 | 17 of 30 |
| Claude Opus 5.5 (via OpenRouter) | low | 23.4 | 4 of 30 |

## Quality ratings

Ten images were rated: 5 religious-art images and 5 phone photos, spread evenly over their part of the subset (the script picks them). The agent that ran this benchmark (Claude) looked at each image at the size the models saw (1024 px long side) and scored every model's tags and its description from 1 to 5. Mike can spot-check any row. The outputs were laid out with model names hidden and in a different shuffled order for each image, and were scored before the names were revealed. All eight runs were scored in one pass so the same yardstick applies to each.

**Tags.**
- **5**: every tag is true of the image, and together they cover the main subject, the key objects and the setting. Nothing important is missing.
- **4**: all true, but a notable element is missing or one tag is weak, imprecise or generic.
- **3**: the main subject is right, but there are two or more generic, unsupported or misleading tags, or several notable elements are missing.
- **2**: several wrong tags, or nearly all generic.
- **1**: mostly wrong.

**Description.**
- **5**: accurate and specific, with no invented detail. One sentence, no "This image shows".
- **4**: accurate but generic, or one minor imprecision.
- **3**: one clear error or invented detail, or the main subject is missing.
- **2**: several errors.
- **1**: wrong or unusable.
- A description of 29 words or more is capped at 4, because the prompt asks for at most 25. (Up to 28 is tolerated.)
- A description that the pipeline cut short at an abbreviation such as "St." is scored on the words that remain. That cut is a bug in `normalize_description`, not the model's fault.

With 10 images, a difference of 0.2 is one point on one image, so read the means as tiers, not as a ranking.

Each cell is tags / description. Outputs were shown with model names hidden and in a shuffled order.

| Image | Gemini 2.5 Flash-Lite | GPT-6 Luna | Claude Haiku 5.5 | Claude Sonnet 5.5 | Claude Opus 5.5 | Claude Haiku 5.5 low | Claude Haiku 5.5 medium | Claude Opus 5.5 low |
|---|---|---|---|---|---|---|---|---|
| `a-single-tear-st-augustine-quote.jpg` | 3 / 3 | 4 / 4 | 5 / 5 | 5 / 5 | 4 / 4 | 5 / 4 | 5 / 5 | 4 / 4 |
| `christ-carrying-the-cross-mosaic-via-dolorosa.jpg` | 4 / 4 | 4 / 4 | 5 / 5 | 4 / 5 | 5 / 5 | 5 / 4 | 5 / 5 | 5 / 5 |
| `crusaders.jpg` | 3 / 3 | 5 / 5 | 4 / 3 | 5 / 4 | 5 / 5 | 4 / 3 | 4 / 3 | 5 / 5 |
| `jesus-painting-2.jpeg` | 5 / 4 | 5 / 5 | 5 / 4 | 5 / 5 | 5 / 5 | 5 / 4 | 5 / 4 | 5 / 5 |
| `lead-us-not-into-temptation-angel-painting.jpg` | 3 / 3 | 4 / 4 | 5 / 5 | 5 / 5 | 5 / 5 | 5 / 5 | 5 / 4 | 5 / 5 |
| `photo-3d1846cdf44a` | 3 / 3 | 5 / 5 | 5 / 5 | 5 / 5 | 5 / 5 | 5 / 5 | 5 / 5 | 5 / 5 |
| `photo-068952cfd87a` | 3 / 3 | 4 / 4 | 4 / 5 | 5 / 4 | 5 / 5 | 4 / 4 | 4 / 4 | 5 / 5 |
| `photo-1281e767a202` | 4 / 4 | 4 / 4 | 5 / 5 | 5 / 5 | 4 / 4 | 5 / 5 | 5 / 5 | 4 / 4 |
| `photo-eb48697aae91` | 3 / 3 | 4 / 4 | 5 / 5 | 4 / 5 | 5 / 5 | 5 / 4 | 5 / 5 | 5 / 5 |
| `photo-b734267705a9` | 4 / 4 | 5 / 5 | 4 / 4 | 5 / 4 | 4 / 4 | 5 / 4 | 5 / 4 | 5 / 5 |

## Jev tag verifier on `openrouter-anthropic-claude-haiku-5.5-effort-low`

30 of 30 photos verified (0 failed). 3 photos changed: 3 tags dropped for a low score, 1 merged away, 0 low-scoring tags kept by the five-tag floor. Overlapping tag pairs put to the merge question: 1. Judged by looking at the photos: 3 correct drop, 1 good merge. Latency p50 345 ms, p95 539 ms, 0 calls over the production timeout of 5 s. Cost $0.094 per 1k images.

| Image | Dropped (probability) | Merged away → kept | Judgement |
|---|---|---|---|
| `christ-cross.jpg` |  | painting → oil painting | painting: good merge (merged into `oil painting`, the more specific tag, and nothing was lost) |
| `photo-86a3bb590f02` | a tag (0.32) |  | tag withheld: correct drop |
| `photo-f3005191de55` | a tag (0.09), a tag (0.34) |  | tag withheld: correct drop; tag withheld: correct drop |

## Jev tag verifier on `openrouter-anthropic-claude-opus-5.5-effort-low`

30 of 30 photos verified (0 failed). 3 photos changed: 3 tags dropped for a low score, 0 merged away, 0 low-scoring tags kept by the five-tag floor. Overlapping tag pairs put to the merge question: 0. Judged by looking at the photos: 1 borderline, 1 correct drop, 1 wrong drop. Latency p50 347 ms, p95 491 ms, 0 calls over the production timeout of 5 s. Cost $0.095 per 1k images.

| Image | Dropped (probability) | Merged away → kept | Judgement |
|---|---|---|---|
| `christ-cross.jpg` | dark sky (0.40) |  | dark sky: borderline (the backdrop is a murky, cloudy dark; `dark background` would have been safer) |
| `photo-068952cfd87a` | a tag (0.49) |  | tag withheld: wrong drop |
| `photo-34c515a098f5` | a tag (0.30) |  | tag withheld: correct drop |

## Jev tag verifier on `openrouter-google-gemini-2.5-flash-lite`

30 of 30 photos verified (0 failed). 8 photos changed: 8 tags dropped for a low score, 1 merged away, 0 low-scoring tags kept by the five-tag floor. Overlapping tag pairs put to the merge question: 1. Judged by looking at the photos: 1 borderline, 6 correct drop, 1 good merge, 1 wrong drop. Latency p50 389 ms, p95 598 ms, 0 calls over the production timeout of 5 s. Cost $0.093 per 1k images.

| Image | Dropped (probability) | Merged away → kept | Judgement |
|---|---|---|---|
| `immaculate-conception.jpg` | globe (0.34) |  | globe: wrong drop (the figure stands on a globe wound by a serpent) |
| `photo-fb7229f1e8e2` | a tag (0.08) |  | tag withheld: correct drop |
| `photo-243193ebb252` | a tag (0.45) |  | tag withheld: correct drop |
| `photo-64ee06212956` | a tag (0.37) |  | tag withheld: correct drop |
| `photo-b509736b2670` | a tag (0.27), a tag (0.42) |  | tag withheld: correct drop; tag withheld: borderline |
| `photo-34c515a098f5` | a tag (0.39) |  | tag withheld: correct drop |
| `photo-7fd962723bec` |  | a tag → a tag | tag withheld: good merge |
| `photo-1956e6025177` | a tag (0.46) |  | tag withheld: correct drop |

## Notes

- The three Claude tiers are within the noise of each other on tags (4.7–4.8) and descriptions (4.6–4.8). Without the 29-word cap, Haiku's description score is 4.7–4.8 as well. Gemini 2.5 Flash-Lite is the clear low (3.5 / 3.4): it often returns generic one-word tags, and on the phone photos it sometimes described a scene the photo doesn't show. GPT-6 Luna sits between (4.4 / 4.4).
- The factual errors that did occur were colors invented for a black-and-white engraving (`red crosses` on the crusaders image, from three different runs) and a material named wrongly in one phone photo. No call was refused, and none needed a correction request.
- Claude Haiku and Sonnet often ignore the prompt's 25-word limit for the description (see the second table). The pipeline only caps characters, so this is harmless today, but the prompt could say "at most 20 words" to pull the average back.
- `normalize_description` keeps only the first sentence, and it treats the period of `St.` as a sentence end. Two descriptions of the St Augustine image (Gemini and Luna) were saved cut off after "St.". That is a bug in `api/src/gallery/domain/tags.py`, outside this ticket.
- Ratings are one rater's judgement on 10 images. They are a screen for obvious gaps, not a benchmark with error bars. The ratings sheet is not committed. The repository is public, so what the models wrote about the phone photos is withheld too.

## Spend

Cap: $0.75. Sum of reported `cost_usd` in the files here: $0.6561 for the analyzers plus $0.0085 for Jev (billed by TypeSafe, not OpenRouter). OpenRouter's own usage counter for the API key moved by $0.6580 since the first run here, which also covers harness trials, repeated runs and failed calls.
