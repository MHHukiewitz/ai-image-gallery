import io
import json
import logging
import sys
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from gallery import observability
from gallery.config import Settings
from gallery.observability import (
    REDACTED,
    JsonFormatter,
    init_observability,
    scrub_breadcrumb,
    scrub_event,
)

SIGNED_URL = "https://abc.supabase.co/storage/v1/object/sign/images/u/1/thumb.webp?token=eyJabc.def"


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """init_observability reconfigures global logging; put it back after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    httpx_level = logging.getLogger("httpx").level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(httpx_level)


def make_record(**kwargs: Any) -> logging.LogRecord:
    fields: dict[str, Any] = {"name": "gallery.test", "levelno": logging.INFO, "msg": "hello"}
    fields["levelname"] = logging.getLevelName(fields["levelno"])
    return logging.makeLogRecord(fields | kwargs)


class TestJsonFormatter:
    def test_emits_valid_json_with_fixed_keys(self) -> None:
        line = JsonFormatter("api").format(make_record(msg="uploaded %s", args=("cat.png",)))

        payload = json.loads(line)

        assert payload["level"] == "INFO"
        assert payload["logger"] == "gallery.test"
        assert payload["msg"] == "uploaded cat.png"
        assert payload["component"] == "api"
        assert payload["ts"].endswith("+00:00")

    def test_includes_extra_fields(self) -> None:
        logger = logging.getLogger("gallery.test.extra")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter("worker"))
        logger.addHandler(handler)
        logger.propagate = False
        logger.setLevel(logging.INFO)

        logger.info("job done", extra={"image_id": 42, "cache_hit": True, "cost_usd": 0.0004})

        payload = json.loads(stream.getvalue())
        assert payload["image_id"] == 42
        assert payload["cache_hit"] is True
        assert payload["cost_usd"] == 0.0004
        assert payload["component"] == "worker"

    def test_one_line_per_record_even_for_multiline_messages(self) -> None:
        line = JsonFormatter("api").format(make_record(msg="first\nsecond"))

        assert "\n" not in line
        assert json.loads(line)["msg"] == "first\nsecond"

    def test_extra_cannot_overwrite_fixed_keys(self) -> None:
        line = JsonFormatter("api").format(make_record(component="spoofed", level="NOPE"))

        payload = json.loads(line)
        assert payload["component"] == "api"
        assert payload["level"] == "INFO"

    def test_unserializable_extras_fall_back_to_str(self) -> None:
        line = JsonFormatter("api").format(make_record(path=io.StringIO))

        assert json.loads(line)["path"] == str(io.StringIO)

    def test_includes_traceback_for_exceptions(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            line = JsonFormatter("api").format(make_record(exc_info=sys.exc_info()))

        assert "ValueError: boom" in json.loads(line)["exception"]


class TestScrubEvent:
    def test_drops_authorization_and_cookie_headers(self) -> None:
        event = {
            "request": {
                "headers": {
                    "Authorization": "Bearer test-jwt",
                    "cookie": "sb=test",
                    "Content-Type": "application/json",
                    "User-Agent": "pytest",
                },
                "cookies": {"sb": "test"},
            }
        }

        scrubbed = scrub_event(event)

        assert scrubbed["request"]["headers"] == {
            "Content-Type": "application/json",
            "User-Agent": "pytest",
        }
        assert "cookies" not in scrubbed["request"]

    def test_drops_sensitive_headers_given_as_pairs(self) -> None:
        event = {"request": {"headers": [["AUTHORIZATION", "Bearer x"], ["Accept", "*/*"]]}}

        assert scrub_event(event)["request"]["headers"] == [["Accept", "*/*"]]

    def test_redacts_token_in_url_and_keeps_other_params(self) -> None:
        event = {"request": {"url": f"{SIGNED_URL}&width=300", "query_string": "token=abc&w=3"}}

        request = scrub_event(event)["request"]

        assert request["url"] == (
            f"https://abc.supabase.co/storage/v1/object/sign/images/u/1/thumb.webp"
            f"?token={REDACTED}&width=300"
        )
        assert request["query_string"] == f"token={REDACTED}&w=3"

    def test_redacts_token_when_it_is_not_the_first_param(self) -> None:
        event = {"request": {"url": "https://x.test/a?w=1&token=secret&h=2"}}

        assert scrub_event(event)["request"]["url"] == f"https://x.test/a?w=1&token={REDACTED}&h=2"

    def test_does_not_touch_params_that_merely_end_in_token(self) -> None:
        event = {"request": {"url": "https://x.test/a?csrf_token=keep&token=drop"}}

        url = scrub_event(event)["request"]["url"]

        assert url == f"https://x.test/a?csrf_token=keep&token={REDACTED}"

    def test_redacts_token_in_parsed_query_string(self) -> None:
        as_dict = scrub_event({"request": {"query_string": {"token": "t", "page": "2"}}})
        as_pairs = scrub_event({"request": {"query_string": [["token", "t"], ["page", "2"]]}})

        assert as_dict["request"]["query_string"] == {"token": REDACTED, "page": "2"}
        assert as_pairs["request"]["query_string"] == [["token", REDACTED], ["page", "2"]]

    def test_redacts_token_in_exception_messages(self) -> None:
        event = {
            "exception": {"values": [{"type": "HTTPStatusError", "value": f"403 for {SIGNED_URL}"}]}
        }

        value = scrub_event(event)["exception"]["values"][0]["value"]

        assert "eyJabc" not in value
        assert f"token={REDACTED}" in value

    def test_scrubs_breadcrumbs_attached_to_the_event(self) -> None:
        event = {"breadcrumbs": {"values": [{"message": f"GET {SIGNED_URL}"}]}}

        crumb = scrub_event(event)["breadcrumbs"]["values"][0]

        assert "eyJabc" not in crumb["message"]

    def test_leaves_events_without_a_request_alone(self) -> None:
        event = {"message": "plain", "level": "error"}

        assert scrub_event(event) == {"message": "plain", "level": "error"}


class TestScrubBreadcrumb:
    def test_redacts_token_in_url_data_and_message(self) -> None:
        crumb = {
            "category": "httpx",
            "message": f"HTTP Request: GET {SIGNED_URL} 200",
            "data": {"url": SIGNED_URL, "http.query": "token=abc&w=1", "status_code": 200},
        }

        scrubbed = scrub_breadcrumb(crumb)

        assert "eyJabc" not in scrubbed["message"]
        assert scrubbed["data"]["url"].endswith(f"thumb.webp?token={REDACTED}")
        assert scrubbed["data"]["http.query"] == f"token={REDACTED}&w=1"
        assert scrubbed["data"]["status_code"] == 200

    def test_drops_sensitive_headers_in_data(self) -> None:
        crumb = {"data": {"headers": {"Authorization": "Bearer x", "Accept": "*/*"}}}

        assert scrub_breadcrumb(crumb)["data"]["headers"] == {"Accept": "*/*"}

    def test_never_discards_the_breadcrumb(self) -> None:
        assert scrub_breadcrumb({"message": "ok"}) == {"message": "ok"}


class TestInitObservability:
    def test_without_dsn_never_initialises_sentry(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        sentry_init = MagicMock()
        monkeypatch.setattr(observability.sentry_sdk, "init", sentry_init)

        init_observability(settings, "api")

        sentry_init.assert_not_called()

    def test_with_dsn_initialises_sentry_with_privacy_options(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SENTRY_DSN", "https://public@o0.ingest.de.sentry.io/0")
        monkeypatch.setenv("SENTRY_ENVIRONMENT", "production")
        monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "0.25")
        monkeypatch.setenv("RELEASE", "abc1234")
        sentry_init = MagicMock()
        global_scope = MagicMock()
        monkeypatch.setattr(observability.sentry_sdk, "init", sentry_init)
        monkeypatch.setattr(observability.sentry_sdk, "get_global_scope", lambda: global_scope)

        init_observability(Settings(), "worker")

        sentry_init.assert_called_once_with(
            dsn="https://public@o0.ingest.de.sentry.io/0",
            environment="production",
            release="abc1234",
            traces_sample_rate=0.25,
            send_default_pii=False,
            max_request_body_size="never",
            before_send=scrub_event,
            before_breadcrumb=scrub_breadcrumb,
        )
        global_scope.set_tag.assert_called_once_with("component", "worker")

    # sentry-sdk 2.72 deprecates send_default_pii in favour of data_collection. We keep the
    # documented privacy setting; moving to data_collection is a separate decision.
    @pytest.mark.filterwarnings("ignore:The send_default_pii option is deprecated")
    def test_sentry_sdk_accepts_the_options(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A mock hides typos in option names, so also run against the real SDK once."""
        monkeypatch.setenv("SENTRY_DSN", "https://public@o0.ingest.de.sentry.io/0")
        sdk = observability.sentry_sdk

        try:
            init_observability(Settings(), "api")
            options = sdk.get_client().options
        finally:
            sdk.get_client().close()
            sdk.init()  # swap in a disabled client so later tests are unaffected

        assert options["send_default_pii"] is False
        assert options["max_request_body_size"] == "never"
        assert options["before_send"] is scrub_event
        assert options["before_breadcrumb"] is scrub_breadcrumb

    def test_logs_json_lines_to_stdout(
        self, capsys: pytest.CaptureFixture[str], settings: Settings
    ) -> None:
        init_observability(settings, "api")

        logging.getLogger("gallery.test").info("ready", extra={"image_id": 7})

        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["msg"] == "ready"
        assert payload["image_id"] == 7
        assert payload["component"] == "api"

    def test_calling_twice_does_not_duplicate_lines(
        self, capsys: pytest.CaptureFixture[str], settings: Settings
    ) -> None:
        init_observability(settings, "api")
        init_observability(settings, "worker")

        logging.getLogger("gallery.test").info("once")

        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["component"] == "worker"

    def test_honours_log_level(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("LOG_LEVEL", "warning")
        init_observability(Settings(), "api")

        logging.getLogger("gallery.test").info("hidden")
        logging.getLogger("gallery.test").warning("shown")

        out = capsys.readouterr().out
        assert "hidden" not in out
        assert "shown" in out

    def test_keeps_foreign_root_handlers(self, settings: Settings) -> None:
        foreign = logging.NullHandler()
        logging.getLogger().addHandler(foreign)

        init_observability(settings, "api")

        assert foreign in logging.getLogger().handlers

    def test_routes_uvicorn_logs_through_the_json_handler(
        self, capsys: pytest.CaptureFixture[str], settings: Settings
    ) -> None:
        init_observability(settings, "api")

        logging.getLogger("uvicorn.error").info("Application startup complete.")

        assert json.loads(capsys.readouterr().out.strip())["logger"] == "uvicorn.error"

    def test_httpx_request_urls_are_not_logged_at_info(self, settings: Settings) -> None:
        init_observability(settings, "api")

        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
