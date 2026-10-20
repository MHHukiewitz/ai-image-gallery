"""Turn a model's text reply into a validated `Analysis`."""

import json
import re

from gallery.domain.models import Analysis
from gallery.domain.tags import MIN_TAGS, normalize_description, normalize_tags

_CODE_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


class InvalidAnalysisError(ValueError):
    """The reply can't be used. The message says why and is safe to send back to the model."""


def parse_analysis(reply: str) -> Analysis:
    """Parse and normalize a JSON reply of the form `{"tags": [...], "description": "..."}`.

    Raises `InvalidAnalysisError` when the reply isn't that object or when too few tags survive
    normalization. Providers that enforce the schema still hit the second case, because the
    schema can't see duplicates or punctuation.
    """
    text = reply.strip()
    fenced = _CODE_FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise InvalidAnalysisError(f"the reply was not valid JSON ({error.msg})") from error

    if not isinstance(payload, dict):
        raise InvalidAnalysisError("the reply was not a JSON object")
    tags = payload.get("tags")
    description = payload.get("description")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise InvalidAnalysisError('"tags" must be a list of strings')
    if not isinstance(description, str):
        raise InvalidAnalysisError('"description" must be a string')

    clean_tags = normalize_tags(tags)
    if len(clean_tags) < MIN_TAGS:
        raise InvalidAnalysisError(
            f"only {len(clean_tags)} usable tags remained after cleanup; at least {MIN_TAGS} "
            "distinct tags of one or two words are required"
        )
    clean_description = normalize_description(description)
    if not clean_description:
        raise InvalidAnalysisError('"description" was empty')
    return Analysis(tags=clean_tags, description=clean_description)
