"""Jittered retries for the Supabase adapters (Storage and PostgREST).

A call that failed for a moment, such as a saturated connection pool or a read that got no answer,
is tried again after a randomized pause. Full jitter spreads the retries of a burst over the whole
backoff window instead of sending them back together, which is what keeps a saturated pool
saturated.

Only call this for work that is safe to repeat. A write whose first attempt may have landed
(an insert) must not go through it.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from postgrest import APIError

# A request that never got an answer. The server may or may not have acted on it.
TRANSIENT_NETWORK_ERRORS = (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException)

# 429 is "too many requests"; the 5xx codes are a gateway or the database failing for a moment.
# Everything else, such as 400, 403 and 404, is a request that a retry can't fix.
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})

# PostgREST's own errors for "the database is not reachable right now", which it sends as 503 or
# 504. PGRST000/001: no connection or an internal database error. PGRST002: the schema cache could
# not be loaded. PGRST003: no pooled connection became free in time.
_TRANSIENT_POSTGREST_CODES = frozenset({"PGRST000", "PGRST001", "PGRST002", "PGRST003"})
# Postgres SQLSTATE classes that mean the server ran out of something or lost a connection: 08 is
# "connection exception" and 53 is "insufficient resources" (53300 is "too many connections").
_TRANSIENT_SQLSTATE_CLASSES = ("08", "53")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many times to try, and how long to wait between tries.

    The pause before attempt n + 1 is drawn uniformly from `0 .. min(cap, base * 2 ** (n - 1))`.
    """

    max_attempts: int
    base_delay_seconds: float
    cap_delay_seconds: float


async def retry_transient[T](
    call: Callable[[], Awaitable[T]],
    *,
    operation: str,
    policy: RetryPolicy,
    transient_reason: Callable[[Exception], str | None],
    logger: logging.Logger,
    message: str,
    sleep: Callable[[float], Awaitable[None]],
    uniform: Callable[[float, float], float],
) -> T:
    """Run `call`, retrying while `transient_reason` says the failure is worth another try.

    `transient_reason` returns a short description of a transient error and None for any other
    error, which is raised at once. After `policy.max_attempts` the last error is raised. Each
    retry is logged at WARNING with `message` and the operation, reason, attempt and pause.
    `sleep` and `uniform` are injected so a test can run the retries without waiting.
    """
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await call()
        except Exception as error:
            reason = transient_reason(error)
            if reason is None or attempt == policy.max_attempts:
                raise
            delay = uniform(
                0, min(policy.cap_delay_seconds, policy.base_delay_seconds * 2 ** (attempt - 1))
            )
            logger.warning(
                message,
                extra={
                    "operation": operation,
                    "status": reason,
                    "attempt": attempt,
                    "delay_seconds": delay,
                },
            )
            await sleep(delay)
    raise AssertionError("unreachable: the last attempt returns or raises")


def _status_in(code: object) -> int | None:
    """Return an HTTP status held in a PostgREST error's `code`, or None for any other code.

    When a reply is not PostgREST's own JSON error (a gateway's 504 page, or Supabase's 429), the
    SDK keeps the HTTP status in `code` as an int. A real SQLSTATE has five characters, so a code
    of three digits can only be a status.
    """
    if isinstance(code, int):
        return code
    if isinstance(code, str) and len(code) == 3 and code.isdigit():
        return int(code)
    return None


def transient_database_reason(error: Exception) -> str | None:
    """Return a short description if retrying a failed PostgREST call could help, else None."""
    if isinstance(error, TRANSIENT_NETWORK_ERRORS):
        return type(error).__name__
    if not isinstance(error, APIError):
        return None
    code = error.code
    status = _status_in(code)
    if status is not None:
        return str(status) if status in TRANSIENT_STATUSES else None
    if isinstance(code, str) and (
        code in _TRANSIENT_POSTGREST_CODES or code.startswith(_TRANSIENT_SQLSTATE_CLASSES)
    ):
        return code
    return None
