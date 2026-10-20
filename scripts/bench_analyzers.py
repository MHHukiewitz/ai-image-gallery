"""Benchmark the image analyzers, and the Jev tag verifier, over a fixed set of photos (#6).

Run it from the repository root. It reads the analyzers from `api/`, so it needs that project's
environment:

    uv run --project api python scripts/bench_analyzers.py \\
        --models openrouter:anthropic/claude-haiku-5.5 openrouter:google/gemini-2.5-flash-lite

Model ids carry an adapter prefix:
- `anthropic:<model>` uses `AnthropicAnalyzer` with `ANTHROPIC_API_KEY`.
- `openrouter:<vendor/model>` uses `OpenAICompatAnalyzer` with `AI_BASE_URL` and `AI_API_KEY`.
- `fake:fake` uses `FakeAnalyzer`. It costs nothing, so it's for trying the harness.

Each run writes `<out-dir>/<model-slug>.json` and regenerates `<out-dir>/summary.md`. A second
mode, `--verify-from <model-slug>`, replays that run's tags through the Jev verifier.

The photos are private, and the repository is public. Images stay out of it, and so do their file
names: a `prod-*` photo is known everywhere as `photo-<first 12 hex of the sha256 of its bytes>`.
Run the models, rate them and verify first, then use `--redact` to strip the tags and descriptions
of those photos from every result file. Their latency, tokens, cost and counts stay. Only then
commit the out-dir.

Spending is capped. `--budget-usd` bounds the total across every run into the same out-dir. The
script adds up each reply's reported `cost_usd` and, for OpenRouter, cross-checks it against the
key's own usage counter, which also sees calls that failed after being billed.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import statistics
import sys
import time
import urllib.request
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast, get_args
from urllib.parse import urlparse

from dotenv import dotenv_values

from gallery.adapters.analyzers.anthropic import AnthropicAnalyzer, Effort
from gallery.adapters.analyzers.fake import FakeAnalyzer
from gallery.adapters.analyzers.openai_compat import OpenAICompatAnalyzer
from gallery.adapters.verifiers.jev import JevTagVerifier
from gallery.domain.errors import AnalyzerError, TagVerificationError
from gallery.domain.models import TagVerdict
from gallery.domain.tags import select_tags
from gallery.ports import ImageAnalyzer

REPO_ROOT = Path(__file__).resolve().parents[1]
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Photos from the private set are named `prod-*`. The rest are public religious art.
PRIVATE_PREFIX = "prod-"
# What a private photo is called in every output: this, then the start of its content hash.
PRIVATE_DISPLAY_PREFIX = "photo-"
PRIVATE_HASH_CHARS = 12
WITHHELD = "withheld"
PRIVATE_SUBSET_SIZE = 19
# The quality ratings cover a smaller spread of the subset, because a person looks at each image.
RATED_ART = 5
RATED_PHOTOS = 5

DEFAULT_BUDGET_USD = 0.75
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_JEV_MODEL = "jev-1.14.0"
# The prompt's word limit for the description. The pipeline itself only caps characters.
PROMPT_DESCRIPTION_WORDS = 25
# The tolerance for a verification call in production (`TAG_VERIFIER_TIMEOUT_SECONDS`).
PRODUCTION_VERIFIER_TIMEOUT_SECONDS = 5.0
TAG_MIN_PROBABILITY = 0.5
# How many calls pass between two reads of the provider's own usage counter.
USAGE_SYNC_EVERY = 5

DISPLAY_NAMES = {
    "claude-haiku-5-5": "Claude Haiku 5.5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-opus-5-5": "Claude Opus 5.5",
    "anthropic/claude-haiku-5.5": "Claude Haiku 5.5",
    "anthropic/claude-sonnet-5.5": "Claude Sonnet 5.5",
    "anthropic/claude-opus-5.5": "Claude Opus 5.5",
    "google/gemini-2.5-flash-lite": "Gemini 2.5 Flash-Lite",
    "openai/gpt-6-luna": "GPT-6 Luna",
}
ADAPTER_SUFFIXES = {
    "anthropic": "Anthropic API",
    "openrouter": "via OpenRouter",
    "fake": "no model",
}

logger = logging.getLogger("bench")


class BudgetExceededError(Exception):
    """The spending cap was reached, so no further request is sent."""


@dataclass(frozen=True)
class ModelSpec:
    adapter: str
    model: str

    @classmethod
    def parse(cls, text: str) -> "ModelSpec":
        adapter, separator, model = text.partition(":")
        if not separator or adapter not in ADAPTER_SUFFIXES or not model:
            choices = ", ".join(f"{name}:<model>" for name in ADAPTER_SUFFIXES)
            raise argparse.ArgumentTypeError(f"{text!r} is not one of {choices}")
        return cls(adapter, model)

    def label(self) -> str:
        name = DISPLAY_NAMES.get(self.model, self.model)
        return f"{name} ({ADAPTER_SUFFIXES[self.adapter]})"

    def slug(self, effort: str | None) -> str:
        base = re.sub(r"[^A-Za-z0-9.]+", "-", f"{self.adapter}-{self.model}").strip("-")
        return f"{base}-effort-{effort}" if effort else base


@dataclass
class CallRecord:
    """What one analyzer call did to one image."""

    image: str
    ok: bool
    latency_ms: int
    # True when the first reply was usable, so neither a correction nor a retry was needed.
    first_try_valid: bool
    corrections: int = 0
    transient_failures: int = 0
    tags: list[str] = field(default_factory=list)
    description: str = ""
    # Kept when `--redact` removes `tags` and `description` of a private photo.
    tag_count: int = 0
    description_words: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    error: str | None = None
    error_retryable: bool | None = None
    error_status_code: int | None = None


class EventCounter(logging.Handler):
    """Counts the analyzers' own warnings, which are the only trace of a correction or retry.

    The call is sequential, so one counter can be reset before each image.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.corrections = 0
        self.transient_failures = 0
        self.last_status_code: int | None = None

    def reset(self) -> None:
        self.corrections = 0
        self.transient_failures = 0
        self.last_status_code = None

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith("analyzer reply unusable"):
            self.corrections += 1
        elif message == "analyzer request failed":
            self.transient_failures += 1
        status_code = getattr(record, "status_code", None)
        if isinstance(status_code, int):
            self.last_status_code = status_code


class Environment:
    """API keys and URLs from `.env` files, overridden by real environment variables."""

    def __init__(self, files: Sequence[Path]) -> None:
        self._values: dict[str, str | None] = {}
        for path in files:
            if path.is_file():
                self._values.update(dotenv_values(path))

    def get(self, name: str) -> str | None:
        return os.environ.get(name) or self._values.get(name) or None

    def require(self, name: str, needed_for: str) -> str:
        value = self.get(name)
        if not value:
            sys.exit(f"{name} is not set (needed for {needed_for}); see --env-file")
        return value


# --- the image subset ---------------------------------------------------------------------------


def spread[T](items: Sequence[T], count: int) -> list[T]:
    """Pick `count` items spaced evenly over `items`, first and last included."""
    if count >= len(items):
        return list(items)
    if count == 1:
        return [items[0]]
    step = (len(items) - 1) / (count - 1)
    return [items[round(index * step)] for index in range(count)]


def is_private(name: str) -> bool:
    """Whether `name` is a private photo, by its original or its redacted name."""
    return name.startswith((PRIVATE_PREFIX, PRIVATE_DISPLAY_PREFIX))


class ImageLibrary:
    """The benchmark images, known by display names that give nothing about a private photo away.

    Public art keeps its file name. A `prod-*` photo becomes `photo-<hash>`, so file names such as
    "selfie" and message timestamps never reach an output file.
    """

    def __init__(self, image_dir: Path) -> None:
        if not image_dir.is_dir():
            sys.exit(f"{image_dir} is not a directory; see --images")
        self._paths: dict[str, Path] = {}
        self._display_by_original: dict[str, str] = {}
        for path in sorted(image_dir.iterdir(), key=lambda item: item.name):
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            display = path.name
            if path.name.startswith(PRIVATE_PREFIX):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                display = f"{PRIVATE_DISPLAY_PREFIX}{digest[:PRIVATE_HASH_CHARS]}"
            self._paths[display] = path
            self._display_by_original[path.name] = display

    def display_names(self) -> list[str]:
        """Every display name, in the order of the original file names."""
        return list(self._paths)

    def display_name(self, name: str) -> str:
        """The display name for an original file name. A name that is already one stays as it is."""
        return self._display_by_original.get(name, name)

    def path(self, name: str) -> Path:
        return self._paths[name]

    def __contains__(self, name: str) -> bool:
        return name in self._paths


def choose_subset(library: ImageLibrary) -> list[str]:
    """All public images, then `PRIVATE_SUBSET_SIZE` private ones spread over the sorted list."""
    names = library.display_names()
    art = [name for name in names if not is_private(name)]
    photos = [name for name in names if is_private(name)]
    return art + spread(photos, PRIVATE_SUBSET_SIZE)


def load_subset(out_dir: Path, library: ImageLibrary) -> list[str]:
    """The recorded subset when `subset.txt` exists, else a fresh choice that is then recorded.

    `rated.txt` lists the images to rate by eye, picked the same way.
    """
    path = out_dir / "subset.txt"
    if path.is_file():
        recorded = [line.strip() for line in path.read_text().splitlines() if line.strip()]
        names = [library.display_name(name) for name in recorded]
    else:
        names = choose_subset(library)
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{name}\n" for name in names))
    rated_path = out_dir / "rated.txt"
    if not rated_path.is_file():
        rated_path.write_text("".join(f"{name}\n" for name in rated_images(names)))
    missing = [name for name in names if name not in library]
    if missing:
        sys.exit(f"{len(missing)} images of {path} are missing from --images, e.g. {missing[0]}")
    return names


def rated_images(subset: Sequence[str]) -> list[str]:
    """The images whose outputs get a 1-5 quality rating: spread over the art, then the photos."""
    art = [name for name in subset if not is_private(name)]
    photos = [name for name in subset if is_private(name)]
    return spread(art, RATED_ART) + spread(photos, RATED_PHOTOS)


# --- spending cap -------------------------------------------------------------------------------


def openrouter_key_usage(base_url: str, api_key: str) -> float | None:
    """Dollars the API key has spent so far, from OpenRouter's own counter. None if unavailable."""
    host = urlparse(base_url).hostname or ""
    if host != "openrouter.ai" and not host.endswith(".openrouter.ai"):
        return None
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/key", headers={"Authorization": f"Bearer {api_key}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            usage = json.load(response)["data"]["usage"]
    except (OSError, KeyError, ValueError, TypeError):
        return None
    return float(usage) if isinstance(usage, int | float) else None


class Budget:
    """Stops the run before spending passes the cap.

    Two measures guard it. The tracked total adds up each reply's `cost_usd`. The counter delta is
    how far the provider's usage counter has moved since the first run into this out-dir. The
    larger one counts, since a failed call may be billed without returning a cost.
    """

    def __init__(
        self,
        *,
        cap_usd: float,
        tracked_before_usd: float,
        counter_baseline_usd: float | None,
        read_counter: Callable[[], float | None],
    ) -> None:
        self.cap_usd = cap_usd
        self._tracked_before = tracked_before_usd
        self._tracked_now = 0.0
        self._baseline = counter_baseline_usd
        self._read_counter = read_counter
        self._counter_delta = 0.0
        self._calls_since_sync = 0
        self.priciest_call_usd = 0.0

    @property
    def tracked_usd(self) -> float:
        return self._tracked_before + self._tracked_now

    @property
    def spent_usd(self) -> float:
        return max(self.tracked_usd, self._counter_delta)

    def record(self, cost_usd: float | None) -> None:
        if cost_usd is not None:
            self._tracked_now += cost_usd
            self.priciest_call_usd = max(self.priciest_call_usd, cost_usd)
        self._calls_since_sync += 1

    def sync_counter(self) -> None:
        usage = self._read_counter() if self._baseline is not None else None
        if usage is not None and self._baseline is not None:
            self._counter_delta = max(0.0, usage - self._baseline)
        self._calls_since_sync = 0

    def counter_delta_usd(self) -> float:
        return self._counter_delta

    def check_before_call(self) -> None:
        """Raises `BudgetExceededError` if one more call of the priciest size could pass the cap."""
        if self._calls_since_sync >= USAGE_SYNC_EVERY:
            self.sync_counter()
        if self.spent_usd + self.priciest_call_usd > self.cap_usd:
            raise BudgetExceededError(
                f"spent ${self.spent_usd:.4f} (tracked ${self.tracked_usd:.4f}, counter "
                f"${self._counter_delta:.4f}) against a cap of ${self.cap_usd:.2f}"
            )


# --- running the analyzers ----------------------------------------------------------------------


class _EffortOpenAICompatAnalyzer(OpenAICompatAnalyzer):
    """OpenAICompatAnalyzer that sends OpenRouter's `reasoning.effort` hint with each request.

    The adapter has no effort setting, so the hint is added to the extra request body it already
    builds for cost reporting. Production would need a real setting if it ever uses this.
    """

    def __init__(self, *, effort: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._effort = effort
        self._extra_body = {**(self._extra_body or {}), "reasoning": {"effort": effort}}

    @property
    def analyzer_id(self) -> str:
        return f"{super().analyzer_id}:effort-{self._effort}"


def build_analyzer(
    spec: ModelSpec, effort: str | None, env: Environment, timeout_seconds: float
) -> ImageAnalyzer:
    match spec.adapter:
        case "anthropic":
            allowed = get_args(Effort)
            if effort is not None and effort not in allowed:
                sys.exit(f"--effort must be one of {allowed} for anthropic: models")
            return AnthropicAnalyzer(
                api_key=env.require("ANTHROPIC_API_KEY", spec.label()),
                model=spec.model,
                # Native models always run with an effort, and the project default is low.
                effort=cast(Effort, effort or "low"),
                timeout=timeout_seconds,
            )
        case "openrouter":
            options: dict[str, Any] = {
                "base_url": env.get("AI_BASE_URL") or OPENROUTER_BASE_URL,
                "api_key": env.require("AI_API_KEY", spec.label()),
                "model": spec.model,
                "timeout": timeout_seconds,
            }
            if effort is None:
                return OpenAICompatAnalyzer(**options)
            return _EffortOpenAICompatAnalyzer(effort=effort, **options)
        case _:
            return FakeAnalyzer()


def effective_effort(spec: ModelSpec, effort: str | None) -> str | None:
    """The effort a run's file name and metadata record: native models always have one."""
    return effort or ("low" if spec.adapter == "anthropic" else None)


async def analyze_image(
    analyzer: ImageAnalyzer, counter: EventCounter, library: ImageLibrary, name: str
) -> CallRecord:
    path = library.path(name)
    data = path.read_bytes()
    # The analyzers re-encode the image, so the type is a formality.
    mime_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    counter.reset()
    started = time.perf_counter()
    try:
        analysis = await analyzer.analyze(data, mime_type)
    except AnalyzerError as error:
        elapsed = round((time.perf_counter() - started) * 1000)
        return CallRecord(
            image=name,
            ok=False,
            latency_ms=elapsed,
            first_try_valid=False,
            corrections=counter.corrections,
            transient_failures=counter.transient_failures,
            error=error.message,
            error_retryable=error.retryable,
            error_status_code=counter.last_status_code,
        )
    elapsed = round((time.perf_counter() - started) * 1000)
    usage = analysis.usage
    return CallRecord(
        image=name,
        ok=True,
        latency_ms=elapsed,
        first_try_valid=counter.corrections == 0,
        corrections=counter.corrections,
        transient_failures=counter.transient_failures,
        tags=list(analysis.tags),
        description=analysis.description,
        tag_count=len(analysis.tags),
        description_words=len(analysis.description.split()),
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        cost_usd=usage.cost_usd if usage else None,
    )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


async def run_model(
    spec: ModelSpec,
    effort: str | None,
    *,
    names: Sequence[str],
    library: ImageLibrary,
    out_dir: Path,
    env: Environment,
    timeout_seconds: float,
    budget: Budget,
    counter: EventCounter,
) -> bool:
    """Run each image through one model and save the results. False if the cap stopped it."""
    analyzer = build_analyzer(spec, effort, env, timeout_seconds)
    recorded_effort = effective_effort(spec, effort)
    slug = spec.slug(recorded_effort)
    meta: dict[str, Any] = {
        "kind": "analyzer-run",
        "label": spec.label(),
        "adapter": spec.adapter,
        "model": spec.model,
        "effort": recorded_effort,
        "effort_is_hint": spec.adapter != "anthropic" and recorded_effort is not None,
        "analyzer_id": analyzer.analyzer_id,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "images_planned": len(names),
        "aborted": None,
    }
    calls: list[CallRecord] = []
    complete = True
    for index, name in enumerate(names, start=1):
        try:
            budget.check_before_call()
        except BudgetExceededError as stop:
            meta["aborted"] = str(stop)
            complete = False
            break
        record = await analyze_image(analyzer, counter, library, name)
        calls.append(record)
        budget.record(record.cost_usd)
        outcome = "ok" if record.ok else f"FAILED ({record.error})"
        print(
            f"[{slug}] {index}/{len(names)} {name[:40]:40} {outcome} {record.latency_ms} ms "
            f"${record.cost_usd or 0:.5f} | spent ${budget.spent_usd:.4f}",
            file=sys.stderr,
        )
        write_json(
            out_dir / f"{slug}.json",
            {"meta": meta, "calls": [asdict(call) for call in calls]},
        )
    budget.sync_counter()
    meta["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    write_json(out_dir / f"{slug}.json", {"meta": meta, "calls": [asdict(c) for c in calls]})
    return complete


# --- the Jev verifier ---------------------------------------------------------------------------


def classify_changes(tags: Sequence[str], verdict: TagVerdict) -> dict[str, Any]:
    """Apply `select_tags` as the worker does, then say why each missing tag went.

    A tag is `merged` when it lost a merge whose winner was kept, else `dropped` for a low score.
    A low-scoring tag the five-tag floor brought back is `restored`.
    """
    final = select_tags(tags, verdict, min_probability=TAG_MIN_PROBABILITY)
    lost_merge = {
        dropped: kept
        for kept, dropped in verdict.merges
        if kept in final
        and verdict.probabilities.get(kept, TAG_MIN_PROBABILITY) >= TAG_MIN_PROBABILITY
    }
    merged = [
        {"tag": tag, "kept": lost_merge[tag]}
        for tag in tags
        if tag not in final and tag in lost_merge
    ]
    dropped = [
        {"tag": tag, "probability": verdict.probabilities.get(tag)}
        for tag in tags
        if tag not in final and tag not in lost_merge
    ]
    restored = [
        {"tag": tag, "probability": verdict.probabilities.get(tag)}
        for tag in final
        if verdict.probabilities.get(tag, TAG_MIN_PROBABILITY) < TAG_MIN_PROBABILITY
    ]
    return {"final_tags": list(final), "merged": merged, "dropped": dropped, "restored": restored}


async def run_verifier(
    source_slug: str,
    *,
    library: ImageLibrary,
    out_dir: Path,
    env: Environment,
    timeout_seconds: float,
    budget: Budget,
) -> None:
    """Replay one run's tags through Jev and record what it would change."""
    source = json.loads((out_dir / f"{source_slug}.json").read_text())
    model = env.get("JEV_MODEL") or DEFAULT_JEV_MODEL
    verifier = JevTagVerifier(
        api_key=env.require("TYPESAFE_API_KEY", "the Jev verifier"),
        model=model,
        timeout=timeout_seconds,
    )
    results: list[dict[str, Any]] = []
    for call in source["calls"]:
        if not call["ok"]:
            continue
        budget.check_before_call()
        data = library.path(call["image"]).read_bytes()
        started = time.perf_counter()
        try:
            verdict = await verifier.verify(data, "image/jpeg", call["tags"])
        except TagVerificationError as error:
            results.append(
                {
                    "image": call["image"],
                    "tags": call["tags"],
                    "error": str(error),
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                }
            )
            print(f"[jev] {call['image'][:40]:40} FAILED ({error})", file=sys.stderr)
            continue
        usage = verdict.usage
        budget.record(usage.cost_usd if usage else None)
        changes = classify_changes(call["tags"], verdict)
        results.append(
            {
                "image": call["image"],
                "tags": call["tags"],
                "probabilities": verdict.probabilities,
                "merge_pairs": [list(pair) for pair in verdict.merges],
                **changes,
                "latency_ms": usage.latency_ms if usage else None,
                "input_tokens": usage.input_tokens if usage else None,
                "cost_usd": usage.cost_usd if usage else None,
            }
        )
        print(
            f"[jev] {call['image'][:40]:40} -{len(changes['dropped'])} dropped "
            f"-{len(changes['merged'])} merged {usage.latency_ms if usage else '?'} ms",
            file=sys.stderr,
        )
        write_json(
            out_dir / f"jev-{source_slug}.json",
            {"meta": _jev_meta(source_slug, model, timeout_seconds), "results": results},
        )
    write_json(
        out_dir / f"jev-{source_slug}.json",
        {"meta": _jev_meta(source_slug, model, timeout_seconds), "results": results},
    )


def _jev_meta(source_slug: str, model: str, timeout_seconds: float) -> dict[str, Any]:
    return {
        "kind": "jev-verification",
        "source": source_slug,
        "model": model,
        "min_probability": TAG_MIN_PROBABILITY,
        "benchmark_timeout_seconds": timeout_seconds,
        "production_timeout_seconds": PRODUCTION_VERIFIER_TIMEOUT_SECONDS,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


# --- summary ------------------------------------------------------------------------------------


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile, so a p95 of 30 samples is a value that was actually measured."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def mean_or_none(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def format_value(value: float | None, template: str) -> str:
    return "n/a" if value is None else template.format(value)


def tag_count(call: dict[str, Any]) -> int:
    """Tags of one call. `--redact` removes the tags of private photos but keeps this count."""
    return call.get("tag_count") or len(call["tags"])


def description_words(call: dict[str, Any]) -> int:
    return call.get("description_words") or len(call["description"].split())


def run_stats(run: dict[str, Any]) -> dict[str, Any]:
    calls = run["calls"]
    good = [call for call in calls if call["ok"]]
    costs = [call["cost_usd"] for call in good if call["cost_usd"] is not None]
    latencies = [call["latency_ms"] for call in good]
    return {
        "calls": len(calls),
        "ok": len(good),
        "success_pct": 100 * len(good) / len(calls) if calls else None,
        "valid_pct": 100 * sum(c["first_try_valid"] for c in calls) / len(calls) if calls else None,
        "cost_per_1k": 1000 * statistics.fmean(costs) if costs else None,
        "total_cost": sum(costs),
        "p50": percentile(latencies, 0.5) if latencies else None,
        "p95": percentile(latencies, 0.95) if latencies else None,
        "max": max(latencies) if latencies else None,
        "mean_tags": mean_or_none([tag_count(call) for call in good]),
        "mean_words": mean_or_none([description_words(call) for call in good]),
        "over_word_limit": sum(description_words(call) > PROMPT_DESCRIPTION_WORDS for call in good),
        "mean_in": mean_or_none([c["input_tokens"] for c in good if c["input_tokens"] is not None]),
        "mean_out": mean_or_none(
            [c["output_tokens"] for c in good if c["output_tokens"] is not None]
        ),
    }


def mean_quality(ratings: dict[str, Any], slug: str) -> tuple[float | None, float | None, int]:
    """Mean tag score, mean description score and the number of images rated for one run."""
    scores = [
        per_model[slug] for per_model in ratings.get("ratings", {}).values() if slug in per_model
    ]
    return (
        mean_or_none([score["tags"] for score in scores]),
        mean_or_none([score["description"] for score in scores]),
        len(scores),
    )


def load_runs(out_dir: Path) -> list[tuple[str, dict[str, Any]]]:
    runs = []
    for path in out_dir.glob("*.json"):
        payload = json.loads(path.read_text())
        if payload.get("meta", {}).get("kind") == "analyzer-run":
            runs.append((path.stem, payload))
    return sorted(runs, key=lambda item: item[1]["meta"]["started_at"])


def render_summary(out_dir: Path, budget_usd: float) -> str:
    runs = [(slug, run) for slug, run in load_runs(out_dir) if run["meta"]["adapter"] != "fake"]
    ratings_path = out_dir / "ratings.json"
    ratings = json.loads(ratings_path.read_text()) if ratings_path.is_file() else {}
    subset_path = out_dir / "subset.txt"
    subset = subset_path.read_text().split() if subset_path.is_file() else []
    lines = [f"# Analyzer benchmark, {out_dir.name}", ""]
    if subset:
        art = sum(not is_private(name) for name in subset)
        lines += [
            f"{len(subset)} images from `subset.txt`: {art} public religious-art images and "
            f"{len(subset) - art} phone photos spread evenly over the sorted list. The images "
            "are not committed. The repository is public, so the photos appear only as "
            f"`{PRIVATE_DISPLAY_PREFIX}<hash>`, and the tags and descriptions the models wrote "
            f"for them are {WITHHELD}. Their latency, tokens, cost and counts are kept.",
            "",
        ]
    lines += [
        "| Model | Effort | $ / 1k images | p50 ms | p95 ms | Success | Valid JSON, 1st try | "
        "Mean tags | Tokens in / out | Quality: tags / description |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    total_tracked = 0.0
    for slug, run in runs:
        meta, stats = run["meta"], run_stats(run)
        total_tracked += stats["total_cost"]
        tags_score, description_score, rated = mean_quality(ratings, slug)
        quality = (
            f"{tags_score:.1f} / {description_score:.1f} (n={rated})"
            if tags_score is not None and description_score is not None
            else "not rated"
        )
        effort = meta["effort"] or "default"
        if meta["effort_is_hint"]:
            effort += " (hint)"
        label = meta["label"] + (" ⚠ stopped early" if meta["aborted"] else "")
        lines.append(
            f"| {label} | {effort} | {format_value(stats['cost_per_1k'], '${:.3f}')} "
            f"| {format_value(stats['p50'], '{:,.0f}')} | {format_value(stats['p95'], '{:,.0f}')} "
            f"| {stats['ok']}/{stats['calls']} | {format_value(stats['valid_pct'], '{:.0f}%')} "
            f"| {format_value(stats['mean_tags'], '{:.1f}')} "
            f"| {format_value(stats['mean_in'], '{:,.0f}')} / "
            f"{format_value(stats['mean_out'], '{:,.0f}')} | {quality} |"
        )
    lines += [
        "",
        "**How to read it.** Cost is the mean `cost_usd` of the successful calls times 1,000. For "
        "OpenRouter models, `cost_usd` is the cost OpenRouter reports for the request; for "
        "native Anthropic calls it is computed from token counts at list price. Latency is "
        "wall-clock time of one `analyze()` call (image re-encoding, any retry and any "
        "correction included), measured one call at a time. Valid JSON counts replies that "
        "needed no correction request. Effort *(hint)* is OpenRouter's `reasoning.effort`, "
        "sent through the OpenAI-compatible adapter; *default* sends no reasoning setting.",
        "",
    ]
    lines += [
        "| Model | Effort | Mean description words | "
        f"Over the prompt's {PROMPT_DESCRIPTION_WORDS}-word limit |",
        "|---|---|---|---|",
    ]
    for _, run in runs:
        stats = run_stats(run)
        effort = run["meta"]["effort"] or "default"
        lines.append(
            f"| {run['meta']['label']} | {effort} | {format_value(stats['mean_words'], '{:.1f}')} "
            f"| {stats['over_word_limit']} of {stats['ok']} |"
        )
    lines.append("")
    quality_section = render_quality(out_dir, ratings, runs)
    if quality_section:
        lines += quality_section
    jev_section = render_jev(out_dir)
    if jev_section:
        lines += jev_section
    if ratings.get("notes"):
        lines += ["## Notes", "", *[f"- {note}" for note in ratings["notes"]], ""]
    ledger_path = out_dir / "ledger.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.is_file() else {}
    jev_cost = sum(
        result.get("cost_usd") or 0
        for path in out_dir.glob("jev-*.json")
        for result in json.loads(path.read_text())["results"]
    )
    lines += [
        "## Spend",
        "",
        f"Cap: ${budget_usd:.2f}. Sum of reported `cost_usd` in the files here: "
        f"${total_tracked:.4f} for the analyzers plus ${jev_cost:.4f} for Jev (billed by "
        "TypeSafe, not OpenRouter). OpenRouter's own usage counter for the API key moved by "
        f"{format_value(ledger.get('counter_delta_usd'), '${:.4f}')} since the first run here, "
        "which also covers harness trials, repeated runs and failed calls.",
        "",
    ]
    return "\n".join(lines)


def render_quality(
    out_dir: Path, ratings: dict[str, Any], runs: list[tuple[str, dict[str, Any]]]
) -> list[str]:
    if not ratings.get("ratings"):
        return []
    labels = {slug: run["meta"]["label"] for slug, run in runs}
    efforts = {slug: run["meta"]["effort"] for slug, run in runs}
    slugs = [slug for slug, _ in runs if any(slug in r for r in ratings["ratings"].values())]
    column = {
        slug: labels[slug].replace(" (via OpenRouter)", "")
        + (f" {efforts[slug]}" if efforts[slug] else "")
        for slug in slugs
    }
    lines = ["## Quality ratings", "", ratings.get("rubric", ""), ""]
    lines += [
        "Each cell is tags / description. Outputs were shown with model names hidden and in a "
        "shuffled order.",
        "",
        "| Image | " + " | ".join(column[slug] for slug in slugs) + " |",
        "|---|" + "---|" * len(slugs),
    ]
    for image, per_model in ratings["ratings"].items():
        cells = [
            f"{per_model[slug]['tags']} / {per_model[slug]['description']}"
            if slug in per_model
            else ""
            for slug in slugs
        ]
        lines.append(f"| `{image}` | " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def render_jev(out_dir: Path) -> list[str]:
    paths = sorted(out_dir.glob("jev-*.json"))
    judgement_path = out_dir / "judgement-jev.json"
    judgement = json.loads(judgement_path.read_text()) if judgement_path.is_file() else {}
    lines: list[str] = []
    for path in paths:
        payload = json.loads(path.read_text())
        if payload.get("meta", {}).get("kind") != "jev-verification":
            continue
        results = payload["results"]
        scored = [r for r in results if "error" not in r]
        failed = len(results) - len(scored)
        latencies = [r["latency_ms"] for r in scored if r["latency_ms"] is not None]
        costs = [r["cost_usd"] for r in scored if r["cost_usd"] is not None]
        dropped = sum(len(r["dropped"]) for r in scored)
        merged = sum(len(r["merged"]) for r in scored)
        restored = sum(len(r["restored"]) for r in scored)
        changed_photos = sum(1 for r in scored if r["dropped"] or r["merged"])
        pairs = sum(len(r["merge_pairs"]) for r in scored)
        source_judgement = judgement.get(payload["meta"]["source"], {})
        tally = Counter(
            entry["verdict"] for entries in source_judgement.values() for entry in entries
        )
        tally_text = ", ".join(f"{count} {verdict}" for verdict, count in sorted(tally.items()))
        p50 = percentile(latencies, 0.5) if latencies else None
        p95 = percentile(latencies, 0.95) if latencies else None
        slow = sum(
            1 for value in latencies if value > payload["meta"]["production_timeout_seconds"] * 1000
        )
        judged_text = f"Judged by looking at the photos: {tally_text}. " if tally_text else ""
        lines += [
            f"## Jev tag verifier on `{payload['meta']['source']}`",
            "",
            f"{len(scored)} of {len(results)} photos verified ({failed} failed). "
            f"{changed_photos} photos changed: {dropped} tags dropped for a low score, {merged} "
            f"merged away, {restored} low-scoring tags kept by the five-tag floor. "
            f"Overlapping tag pairs put to the merge question: {pairs}. "
            f"{judged_text}"
            f"Latency p50 {format_value(p50, '{:,.0f}')} ms, "
            f"p95 {format_value(p95, '{:,.0f}')} ms, "
            f"{slow} calls over the production timeout of "
            f"{payload['meta']['production_timeout_seconds']:.0f} s. "
            f"Cost {format_value(1000 * statistics.fmean(costs) if costs else None, '${:.3f}')} "
            "per 1k images.",
            "",
            "| Image | Dropped (probability) | Merged away → kept | Judgement |",
            "|---|---|---|---|",
        ]
        for result in scored:
            if not (result["dropped"] or result["merged"]):
                continue
            dropped_text = ", ".join(
                f"{item['tag'] or 'a tag'} ({item['probability']:.2f})"
                for item in result["dropped"]
            )
            merged_text = ", ".join(
                f"{item['tag'] or 'a tag'} → {item['kept'] or 'a tag'}" for item in result["merged"]
            )
            notes = "; ".join(
                f"{entry['tag'] or 'tag ' + WITHHELD}: {entry['verdict']}"
                + (f" ({entry['note']})" if entry.get("note") else "")
                for entry in source_judgement.get(result["image"], [])
            )
            lines.append(f"| `{result['image']}` | {dropped_text} | {merged_text} | {notes} |")
        lines.append("")
    return lines


# --- redaction ----------------------------------------------------------------------------------


def _blank_pairs(items: list[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    """Keep each item's numbers and drop its tag text."""
    return [
        {key: (None if key in keys else value) for key, value in item.items()} for item in items
    ]


def redact_results(out_dir: Path, library: ImageLibrary) -> None:
    """Make an out-dir safe to commit to a public repository. Safe to run again.

    Private photos get their display names. The tags and descriptions the models wrote for them go,
    as do the tag texts and notes of the Jev and judgement files. Counts, scores, latency, tokens
    and cost stay, so every table in `summary.md` can still be built.
    """
    for filename in ("subset.txt", "rated.txt"):
        path = out_dir / filename
        if path.is_file():
            names = [library.display_name(line.strip()) for line in path.read_text().splitlines()]
            path.write_text("".join(f"{name}\n" for name in names if name))
    for path in sorted(out_dir.glob("*.json")):
        payload = json.loads(path.read_text())
        kind = payload.get("meta", {}).get("kind")
        if kind == "analyzer-run":
            for call in payload["calls"]:
                call["image"] = library.display_name(call["image"])
                if call["tags"] or "tag_count" not in call:
                    call["tag_count"] = len(call["tags"])
                if call["description"] or "description_words" not in call:
                    call["description_words"] = len(call["description"].split())
                if is_private(call["image"]):
                    call["tags"] = []
                    call["description"] = ""
                    call["text_withheld"] = True
        elif kind == "jev-verification":
            for result in payload["results"]:
                result["image"] = library.display_name(result["image"])
                if not is_private(result["image"]):
                    continue
                result["text_withheld"] = True
                result["tags"] = []
                if "error" in result:
                    continue
                result["probabilities"] = {}
                result["final_tags"] = []
                result["merge_pairs"] = [[None, None] for _ in result["merge_pairs"]]
                result["dropped"] = _blank_pairs(result["dropped"], "tag")
                result["merged"] = _blank_pairs(result["merged"], "tag", "kept")
                result["restored"] = _blank_pairs(result["restored"], "tag")
        elif path.name == "ratings.json":
            payload["ratings"] = {
                library.display_name(image): scores for image, scores in payload["ratings"].items()
            }
        elif path.name == "judgement-jev.json":
            payload = {
                source: {
                    library.display_name(image): [
                        {"tag": None, "verdict": entry["verdict"], "note": None}
                        if is_private(library.display_name(image))
                        else entry
                        for entry in entries
                    ]
                    for image, entries in per_image.items()
                }
                for source, per_image in payload.items()
            }
        else:
            continue
        write_json(path, payload)


# --- command line -------------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--models", nargs="+", type=ModelSpec.parse, default=[], metavar="ID")
    parser.add_argument(
        "--effort",
        choices=["low", "medium"],
        help="native effort for anthropic: models, OpenRouter's reasoning.effort hint for "
        "openrouter: models. Omitted, openrouter: models send no reasoning setting and "
        "anthropic: models run at low.",
    )
    parser.add_argument("--images", type=Path, default=REPO_ROOT / "bench" / "images")
    parser.add_argument("--limit", type=int, help="use only the first N images of the subset")
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--date", default=date.today().isoformat(), help="names the out-dir")
    parser.add_argument(
        "--env-file",
        type=Path,
        action="append",
        help="a .env file with the keys (repeatable). Default: .env and api/.env of the repo.",
    )
    parser.add_argument("--budget-usd", type=float, default=DEFAULT_BUDGET_USD)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--verify-from", metavar="MODEL_SLUG", help="run that run's tags through Jev instead"
    )
    parser.add_argument("--summary-only", action="store_true", help="rebuild summary.md, no calls")
    parser.add_argument(
        "--redact",
        action="store_true",
        help="rename private photos and strip the text written about them from every result "
        "file, then rebuild summary.md. Do this before committing.",
    )
    return parser.parse_args(argv)


def record_spend(out_dir: Path, budget: Budget) -> None:
    """Note in `ledger.json` how much the provider's counter has moved since the first run."""
    path = out_dir / "ledger.json"
    ledger = json.loads(path.read_text()) if path.is_file() else {}
    ledger["counter_delta_usd"] = round(budget.counter_delta_usd(), 6)
    ledger["tracked_cost_usd"] = round(budget.tracked_usd, 6)
    ledger["cap_usd"] = budget.cap_usd
    write_json(path, ledger)


def make_budget(
    args: argparse.Namespace, out_dir: Path, env: Environment, rerun: set[str]
) -> Budget:
    """Build the cap, remembering where the provider's counter stood at the first run here."""
    base_url = env.get("AI_BASE_URL") or OPENROUTER_BASE_URL
    api_key = env.get("AI_API_KEY") or ""

    def read_counter() -> float | None:
        return openrouter_key_usage(base_url, api_key)

    ledger_path = out_dir / "ledger.json"
    baseline: float | None
    if ledger_path.is_file():
        baseline = json.loads(ledger_path.read_text()).get("openrouter_counter_baseline_usd")
    else:
        baseline = read_counter() if api_key else None
        write_json(ledger_path, {"openrouter_counter_baseline_usd": baseline})
    tracked_before = sum(
        call["cost_usd"] or 0
        for slug, run in load_runs(out_dir)
        if slug not in rerun
        for call in run["calls"]
    )
    budget = Budget(
        cap_usd=args.budget_usd,
        tracked_before_usd=tracked_before,
        counter_baseline_usd=baseline,
        read_counter=read_counter,
    )
    budget.sync_counter()
    return budget


async def main_async(args: argparse.Namespace) -> int:
    out_dir = args.out_dir or REPO_ROOT / "bench" / "results" / args.date
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.summary_only:
        (out_dir / "summary.md").write_text(render_summary(out_dir, args.budget_usd))
        return 0
    if args.redact:
        redact_results(out_dir, ImageLibrary(args.images))
        (out_dir / "summary.md").write_text(render_summary(out_dir, args.budget_usd))
        return 0

    env_files = args.env_file or [REPO_ROOT / ".env", REPO_ROOT / "api" / ".env"]
    env = Environment(env_files)
    library = ImageLibrary(args.images)
    names = load_subset(out_dir, library)
    if args.limit:
        names = names[: args.limit]

    counter = EventCounter()
    capture = logging.getLogger("gallery.adapters")
    capture.addHandler(counter)
    capture.propagate = False

    rerun = {spec.slug(effective_effort(spec, args.effort)) for spec in args.models}
    budget = make_budget(args, out_dir, env, rerun)
    exit_code = 0
    try:
        if args.verify_from:
            await run_verifier(
                args.verify_from,
                library=library,
                out_dir=out_dir,
                env=env,
                timeout_seconds=args.timeout,
                budget=budget,
            )
        else:
            if not args.models:
                sys.exit("give --models, --verify-from or --summary-only")
            for spec in args.models:
                finished = await run_model(
                    spec,
                    args.effort,
                    names=names,
                    library=library,
                    out_dir=out_dir,
                    env=env,
                    timeout_seconds=args.timeout,
                    budget=budget,
                    counter=counter,
                )
                if not finished:
                    print(f"STOPPED by the spending cap: {spec.label()}", file=sys.stderr)
                    exit_code = 3
                    break
    except BudgetExceededError as stop:
        print(f"STOPPED by the spending cap: {stop}", file=sys.stderr)
        exit_code = 3
    finally:
        budget.sync_counter()
        record_spend(out_dir, budget)
        (out_dir / "summary.md").write_text(render_summary(out_dir, args.budget_usd))
        print(
            f"spent ${budget.spent_usd:.4f} of ${budget.cap_usd:.2f} "
            f"(tracked ${budget.tracked_usd:.4f}); summary: {out_dir / 'summary.md'}",
            file=sys.stderr,
        )
    return exit_code


def main() -> int:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    sys.exit(main())
