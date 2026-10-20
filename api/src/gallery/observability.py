"""JSON logging and Sentry setup, shared by the API and the worker.

This is the only module that imports ``sentry_sdk``. The rest of the code just logs: Sentry's
default logging integration turns ``ERROR`` records into events.
"""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any, Literal

import sentry_sdk

from gallery.config import Settings

Component = Literal["api", "worker"]

REDACTED = "[redacted]"

_DROPPED_HEADERS = frozenset({"authorization", "cookie"})
# Signed storage URLs carry their access token in a `token=` query parameter.
_TOKEN_PARAM = re.compile(r"(?P<lead>(?:^|[?&])token=)[^&#\s\"']*")
# Attributes every LogRecord has. Anything else on a record came in through `extra={...}`.
# `color_message` is uvicorn's ANSI-colored copy of the message, which is noise in JSON.
_IGNORED_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "color_message",
}
_HANDLER_MARKER = "_gallery_json_handler"


class JsonFormatter(logging.Formatter):
    """Formats each record as one JSON object per line, including its ``extra`` fields."""

    def __init__(self, component: str) -> None:
        super().__init__()
        self._component = component

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "component": self._component,
        }
        for key, value in record.__dict__.items():
            # setdefault: an `extra` field must not overwrite the fixed keys above.
            if key not in _IGNORED_RECORD_FIELDS:
                payload.setdefault(key, value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def redact_tokens(text: str) -> str:
    """Replace the value of any ``token=`` query parameter in ``text``."""
    return _TOKEN_PARAM.sub(lambda match: match["lead"] + REDACTED, text)


def _drop_sensitive_headers(headers: Any) -> Any:
    """Remove Authorization and Cookie from headers given as a dict or a list of pairs."""
    if isinstance(headers, dict):
        return {k: v for k, v in headers.items() if str(k).lower() not in _DROPPED_HEADERS}
    if isinstance(headers, list):
        return [
            pair
            for pair in headers
            if not (isinstance(pair, list | tuple) and str(pair[0]).lower() in _DROPPED_HEADERS)
        ]
    return headers


def _redact_query(query: Any) -> Any:
    """Redact ``token`` in a query string given as text, a dict or a list of pairs."""
    if isinstance(query, str):
        return redact_tokens(query)
    if isinstance(query, dict):
        return {k: REDACTED if k == "token" else v for k, v in query.items()}
    if isinstance(query, list):
        return [
            [pair[0], REDACTED] if isinstance(pair, list | tuple) and pair[0] == "token" else pair
            for pair in query
        ]
    return query


def _redact_field(container: dict[str, Any], key: str) -> None:
    if isinstance(container.get(key), str):
        container[key] = redact_tokens(container[key])


def scrub_breadcrumb(
    breadcrumb: dict[str, Any], hint: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Sentry ``before_breadcrumb``: keep the breadcrumb, minus signed-URL tokens and secrets."""
    _redact_field(breadcrumb, "message")
    data = breadcrumb.get("data")
    if isinstance(data, dict):
        for key in data:
            _redact_field(data, key)
        if "headers" in data:
            data["headers"] = _drop_sensitive_headers(data["headers"])
    return breadcrumb


def scrub_event(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any]:
    """Sentry ``before_send``: drop Authorization and Cookie, redact ``token=`` values.

    Exception messages are scrubbed too, because httpx errors include the full request URL.
    """
    request = event.get("request")
    if isinstance(request, dict):
        if "headers" in request:
            request["headers"] = _drop_sensitive_headers(request["headers"])
        request.pop("cookies", None)
        _redact_field(request, "url")
        if "query_string" in request:
            request["query_string"] = _redact_query(request["query_string"])

    _redact_field(event, "message")
    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        _redact_field(logentry, "message")
    exceptions = event.get("exception")
    if isinstance(exceptions, dict):
        for exception in exceptions.get("values") or []:
            _redact_field(exception, "value")

    breadcrumbs = event.get("breadcrumbs")
    crumbs = breadcrumbs.get("values") if isinstance(breadcrumbs, dict) else breadcrumbs
    for crumb in crumbs or []:
        scrub_breadcrumb(crumb)
    return event


def _configure_logging(level: str, component: Component) -> None:
    root = logging.getLogger()
    # Replace only our own handler from an earlier call, so other handlers (pytest's) survive.
    for existing in [h for h in root.handlers if getattr(h, _HANDLER_MARKER, False)]:
        root.removeHandler(existing)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(component))
    setattr(handler, _HANDLER_MARKER, True)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs plain-text handlers before it imports the app. Hand its records to ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # httpx logs every request URL at INFO, and signed storage URLs carry their token there.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def init_observability(settings: Settings, component: Component) -> None:
    """Set up JSON logging on stdout and, when ``SENTRY_DSN`` is set, Sentry.

    Without a DSN, Sentry is never initialised and nothing leaves the process.
    """
    _configure_logging(settings.LOG_LEVEL, component)
    if not settings.SENTRY_DSN:
        return
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.SENTRY_ENVIRONMENT,
        release=settings.RELEASE,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        send_default_pii=False,
        # Upload requests carry image bytes, which must never reach Sentry.
        max_request_body_size="never",
        before_send=scrub_event,
        before_breadcrumb=scrub_breadcrumb,
    )
    # The API and worker share one Sentry project and are told apart by this tag.
    sentry_sdk.get_global_scope().set_tag("component", component)
