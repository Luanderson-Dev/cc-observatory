"""The Discord sender: alerts to the Discord webhook, from an outbox (ADRs 0007 and 0046).

The writer stores each alert that fires with its Discord delivery ``pending`` when a
webhook is configured (ADR 0042): the ``alerts`` table is the outbox. This sender runs
on a daemon thread of its own and never touches the database: every
``poll_interval_s`` (5 s) it hands the writer a round (``Writer.submit_discord_round``)
and waits up to ``round_timeout_s`` for its answer. In one transaction the writer
expires the deliveries still pending ``EXPIRE_AFTER_MS`` (1 h) after their alert fired,
which are never sent, and reads up to ``ALERTS_PER_MESSAGE`` (10) deliveries due, the
earliest first, with their sessions' ``cwd``. The sender posts them as one message
(``ccobs.domain.discord_message``) to the webhook with ``?wait=true``, so that Discord
answers only once the message exists, and hands the outcome back to the writer
(``Writer.submit_discord_results``), which records it in one transaction. Every alert of
a message gets the same outcome:

- 2xx: ``sent``.
- 429: still ``pending``, attempts unchanged, due again after Discord's ``retry_after``
  (the JSON body's or the ``Retry-After`` header's, the longer one when both say; 5 s
  when neither does; at most ``RETRY_AFTER_MAX_S``, 15 min). No message goes out before
  then, timed on the monotonic clock (a wall clock stepped back cannot stretch the
  pause), and the expiry still runs.
- 5xx, a timeout, no connection or any other transport error: one more attempt; due
  again after ``BACKOFF_INITIAL_S`` (5 s) doubling per attempt up to ``BACKOFF_MAX_S``
  (5 min); ``failed`` once ``MAX_ATTEMPTS`` (5) attempts were made.
- 401, 403 or 404: the webhook is gone or refused, so ``failed``, and sending is
  suspended until the app restarts (``suspended_status``, for ``ccobs doctor``): Discord
  counts invalid requests toward a Cloudflare ban of the IP. The expiry still runs.
- Any other status (a redirect, which is never followed, another 4xx): ``failed``; a
  retry would be refused the same way.

Delivery is at least once: an app that dies between the POST and the write of its
outcome sends the same alerts again after the restart. The writer, not the sender, counts
the deliveries it expires (``Writer``'s ``on_discord_expired``): a round the sender gave
up on may still have run.

The settings screen's "Send test message" button (``send_connection_test``) posts one fixed
message (``connection_test_payload``) through the same client, at most once per
``TEST_INTERVAL_S`` (10 s), on the caller's thread; it is not an alert, nothing is
stored, and its outcome never suspends the sender. Its status line
(``connection_test_status``) says what the status means: only 401, 403 and 404 point at
the webhook URL.

HTTP: a sync ``httpx.Client`` of its own, with ``timeout_s`` (5 s) per phase, no
redirects, and ``trust_env=True``, unlike every other client of the app: this is the
app's only connection that leaves the machine, so ``HTTPS_PROXY`` and ``NO_PROXY`` (and
``SSL_CERT_FILE``) from the container's environment apply to it, for a network that
reaches Discord only through a proxy.

The webhook URL is a secret (``SecretStr``); only ``_post`` reads its value. Logs hold
status codes and exception type names only: never the URL, a message, a response body
or an exception's text (httpx puts the URL in its exceptions' messages). httpx itself
logs each request's URL at INFO and httpcore traces at DEBUG: a filter on their loggers
(``hide_webhook_urls``) drops every record that names a webhook path, and the app's log
handler carries the same filter (``WEBHOOK_URL_FILTER``), for any other logger.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Final, Literal, Protocol

import httpx
from pydantic import SecretStr

import ccobs
from ccobs.domain.app_metrics import DeliveryResult
from ccobs.domain.discord_message import (
    ALERTS_PER_MESSAGE,
    DiscordAlert,
    Payload,
    alerts_payload,
    connection_test_payload,
)
from ccobs.server.db.repository import AlertView, DeliveryUpdate, DueDeliveries
from ccobs.server.ingest import Clock, wall_clock_ms
from ccobs.server.writer import DiscordUnavailableError

__all__ = [
    "BACKOFF_INITIAL_S",
    "BACKOFF_MAX_S",
    "EXPIRE_AFTER_MS",
    "HTTP_TIMEOUT_S",
    "MAX_ATTEMPTS",
    "POLL_INTERVAL_S",
    "REFUSED_STATUSES",
    "RETRY_AFTER_MAX_S",
    "ROUND_TIMEOUT_S",
    "TEST_INTERVAL_S",
    "WEBHOOK_URL_FILTER",
    "ConnectionTestResult",
    "DiscordSender",
    "DiscordTarget",
    "RoundResult",
    "connection_test_status",
    "hide_webhook_urls",
]

_log = logging.getLogger(__name__)

POLL_INTERVAL_S: Final = 5.0
HTTP_TIMEOUT_S: Final = 5.0
ROUND_TIMEOUT_S: Final = 5.0
BACKOFF_INITIAL_S: Final = 5.0
BACKOFF_MAX_S: Final = 300.0
MAX_ATTEMPTS: Final = 5
RETRY_AFTER_MAX_S: Final = 900.0
EXPIRE_AFTER_MS: Final = 3_600_000
TEST_INTERVAL_S: Final = 10.0
# The statuses that mean the webhook is gone or refused (ADR 0046).
REFUSED_STATUSES: Final = frozenset({401, 403, 404})
_RATE_LIMITED: Final = 429
_SERVER_ERROR: Final = 500
# A 429's body holds retry_after and a few short fields; anything longer is not read.
_BODY_MAX_BYTES: Final = 64 * 1024
_MS_PER_S: Final = 1000
_WAIT: Final = {"wait": "true"}
_USER_AGENT: Final = f"cc-observatory/{ccobs.__version__}"
# The loggers of httpx and httpcore: the first logs each request's URL at INFO. A test
# imports every module of both and fails if a logger is missing here.
_HTTP_LOGGERS: Final = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)
_WEBHOOK_PATH: Final = "/webhooks/"

# What a round did: nothing due (idle), a message posted with that outcome, nothing
# posted while rate limited (paused) or suspended, or no answer from the writer.
type RoundResult = Literal[
    "idle", "sent", "retried", "rate_limited", "failed", "suspended", "paused", "unavailable"
]
type _Outcome = Literal["sent", "rate_limited", "retry", "refused", "rejected"]
_ROUND_RESULTS: Final[dict[_Outcome, RoundResult]] = {
    "sent": "sent",
    "rate_limited": "rate_limited",
    "refused": "suspended",
    "rejected": "failed",
}
type _ConnectionTestKind = Literal["sent", "refused", "unreachable", "too_soon"]


class DiscordTarget(Protocol):
    """What the Discord sender needs of the writer."""

    def submit_discord_round(
        self, now_ms: int, *, expire_before_ms: int, limit: int
    ) -> Future[DueDeliveries]: ...

    def submit_discord_results(self, updates: Sequence[DeliveryUpdate]) -> None: ...


@dataclass(frozen=True, kw_only=True)
class _Attempt:
    """One POST's outcome: a status when Discord answered, else what went wrong (an
    exception type name, or ``timeout``), and a 429's wait in seconds."""

    outcome: _Outcome
    status: int | None = None
    error: str | None = None
    retry_after_s: float = BACKOFF_INITIAL_S


@dataclass(frozen=True, kw_only=True)
class ConnectionTestResult:
    """What the test button's POST got: sent, refused (a non-2xx ``status``),
    unreachable (``reason``: ``timeout``, ``connection error`` or ``error``), or not
    sent because the last test was less than ``TEST_INTERVAL_S`` ago."""

    kind: _ConnectionTestKind
    status: int | None = None
    reason: str | None = None


class _WebhookFilter(logging.Filter):
    """Drops every record whose message names a webhook path: the URL holds the
    webhook's token."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return False  # a record that does not format is not worth the risk
        return _WEBHOOK_PATH not in message


# On the loggers of httpx and httpcore, and on the app's log handler (ADR 0046).
WEBHOOK_URL_FILTER: Final = _WebhookFilter()


def hide_webhook_urls() -> None:
    """Keep httpx's and httpcore's own log records of the webhook out of every log: a
    filter on each of their loggers (a logger's filters do not apply to its children's
    records, so each is named). Idempotent."""
    for name in _HTTP_LOGGERS:
        logger = logging.getLogger(name)
        if WEBHOOK_URL_FILTER not in logger.filters:
            logger.addFilter(WEBHOOK_URL_FILTER)


def connection_test_status(result: ConnectionTestResult) -> str:
    """The status line the settings screen shows for a test; never the URL."""
    if result.kind == "sent":
        return "Test message sent."
    if result.kind == "too_soon":
        return "Wait a few seconds before sending another test."
    if result.kind == "unreachable":
        return f"Could not reach Discord ({result.reason})."
    status = result.status
    if status == _RATE_LIMITED:
        return "Discord is rate limiting the webhook. Try again in a minute."
    if status is not None and status >= _SERVER_ERROR:
        return f"Discord had an error (status {status}). Try again later."
    if status in REFUSED_STATUSES:
        return (
            f"Discord did not accept the test message (status {status})."
            " Check CCOBS_DISCORD_WEBHOOK_URL."
        )
    return f"Discord did not accept the test message (status {status})."


def _seconds(value: object) -> float | None:
    """A positive, finite number of seconds, or None."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _retry_after(response: httpx.Response, body: bytes) -> float:
    """How long a 429 asks to wait: the longer of the body's ``retry_after`` and the
    ``Retry-After`` header, 5 s when neither is valid, at most ``RETRY_AFTER_MAX_S``."""
    found = [_seconds(response.headers.get("retry-after"))]
    try:
        document: object = json.loads(body)
    except (ValueError, RecursionError):
        document = None
    if isinstance(document, dict):
        found.append(_seconds(document.get("retry_after")))
    waits = [value for value in found if value is not None]
    return min(max(waits, default=BACKOFF_INITIAL_S), RETRY_AFTER_MAX_S)


def _backoff_s(attempts: int) -> float:
    """The wait after the ``attempts``-th failed attempt: 5 s doubling, up to 5 min."""
    return min(BACKOFF_INITIAL_S * 2.0 ** (attempts - 1), BACKOFF_MAX_S)


def _discord_alert(view: AlertView) -> DiscordAlert:
    alert = view.alert
    return DiscordAlert(
        rule=alert.rule,
        value=alert.value,
        threshold=alert.threshold,
        session_id=alert.session_id,
        cwd=view.cwd,
    )


class DiscordSender:
    """Sends the alerts' Discord deliveries (see the module's docstring).

    ``webhook_url`` is ``CCOBS_DISCORD_WEBHOOK_URL``, ``host`` the host label, ``app_url``
    the app as the browser opens it (``CCOBS_APP_URL``) and ``include_project`` whether
    messages name the project. ``clock`` gives the wall-clock time in ms (the hook
    route's), for the rows; ``monotonic`` times the 429 pause and the test button's
    interval, which a wall clock that steps must not stretch; ``wait(timeout_s)`` waits
    between rounds and returns True to stop (the stop event by default). ``transport``
    replaces httpx's (tests pass an ``httpx.MockTransport``), and ``on_delivery``
    receives each round's results with their number of alerts. ``run`` is the loop and
    ``send_once`` one round; ``start`` runs the loop on a daemon thread,
    ``request_stop`` tells it to end, and ``stop`` ends it and closes the HTTP client.
    """

    def __init__(
        self,
        writer: DiscordTarget,
        *,
        webhook_url: SecretStr,
        host: str,
        app_url: str,
        include_project: bool,
        poll_interval_s: float = POLL_INTERVAL_S,
        timeout_s: float = HTTP_TIMEOUT_S,
        round_timeout_s: float = ROUND_TIMEOUT_S,
        clock: Clock = wall_clock_ms,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[float], bool] | None = None,
        transport: httpx.BaseTransport | None = None,
        on_delivery: Callable[[DeliveryResult, int], None] | None = None,
    ) -> None:
        for name, value in (
            ("poll_interval_s", poll_interval_s),
            ("timeout_s", timeout_s),
            ("round_timeout_s", round_timeout_s),
        ):
            if not value > 0:
                raise ValueError(f"{name} must be positive, got {value}")
        hide_webhook_urls()
        self.host: Final = host
        self.app_url: Final = app_url
        self.include_project: Final = include_project
        self.poll_interval_s: Final = poll_interval_s
        self._webhook_url: Final = webhook_url
        self._round_timeout_s: Final = round_timeout_s
        self._writer = writer
        self._clock = clock
        self._monotonic = monotonic
        self._on_delivery = on_delivery
        # The app's only client that leaves the machine, and the only one that trusts
        # the environment: HTTPS_PROXY and NO_PROXY apply (ADR 0046).
        self._client: Final = httpx.Client(
            timeout=timeout_s,
            transport=transport,
            trust_env=True,
            follow_redirects=False,
            headers={"user-agent": _USER_AGENT},
        )
        self._stopping = threading.Event()
        self._wait: Callable[[float], bool] = wait or self._stopping.wait
        self._thread: threading.Thread | None = None
        # The sender's thread changes these; other threads only read them.
        self._suspended_status: int | None = None
        # On the monotonic clock, in seconds: no message before then (a 429's wait).
        self._paused_until = -math.inf
        self._test_lock = threading.Lock()
        self._last_test: float | None = None

    @property
    def suspended_status(self) -> int | None:
        """The status (401, 403 or 404) that suspended sending until the app restarts,
        or None while it sends; never the URL."""
        return self._suspended_status

    def start(self) -> None:
        """Run the loop on a daemon thread of its own."""
        if self._thread is not None:
            raise RuntimeError("the Discord sender is already started")
        self._thread = threading.Thread(target=self.run, name="ccobs-discord", daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def request_stop(self) -> None:
        """Tell the loop to end, interrupting its wait; ``stop`` then joins it."""
        self._stopping.set()

    def stop(self, timeout_s: float) -> None:
        """Stop the loop, interrupting its wait, join the thread for ``timeout_s`` and
        close the HTTP client."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                _log.warning("discord sender did not stop within %.1f s", timeout_s)
        self._client.close()

    def run(self) -> None:
        """Wait, send, repeat, until ``wait`` says stop."""
        while not self._wait(self.poll_interval_s):
            try:
                self.send_once()
            except Exception as error:
                # The clock, the writer or a bug: the type only, never its text.
                _log.error("discord round failed (%s)", type(error).__name__)

    # One round.

    def send_once(self) -> RoundResult:
        """Expire, read the deliveries due and post them as one message."""
        now_ms = self._clock()
        paused = self._monotonic() < self._paused_until
        held = self._suspended_status is not None or paused
        future = self._writer.submit_discord_round(
            now_ms,
            expire_before_ms=now_ms - EXPIRE_AFTER_MS,
            limit=0 if held else ALERTS_PER_MESSAGE,
        )
        try:
            due = future.result(timeout=self._round_timeout_s)
        except FutureTimeoutError:
            future.cancel()
            _log.warning("discord round skipped: no answer from the writer in time")
            return "unavailable"
        except DiscordUnavailableError:
            _log.warning("discord round skipped: the writer could not read the deliveries")
            return "unavailable"
        # Expired deliveries are the writer's to log and count (on_discord_expired).
        if self._suspended_status is not None:
            return "suspended"
        if held:
            return "paused"
        if not due.alerts:
            return "idle"
        payload = alerts_payload(
            [_discord_alert(view) for view in due.alerts],
            host=self.host,
            app_url=self.app_url,
            include_project=self.include_project,
        )
        attempt = self._post(payload)
        return self._settle(due.alerts, attempt, self._clock())

    def _settle(self, alerts: Sequence[AlertView], attempt: _Attempt, now_ms: int) -> RoundResult:
        """Hand each alert's new delivery state to the writer and count it."""
        results: Counter[DeliveryResult] = Counter()
        updates = []
        for view in alerts:
            update, result = self._update(view, attempt, now_ms)
            updates.append(update)
            results[result] += 1
        self._writer.submit_discord_results(updates)
        for result, count in results.items():
            self._count(result, count)
        self._log_attempt(attempt, len(alerts))
        if attempt.outcome == "retry":
            return "retried" if results["retried"] else "failed"
        return _ROUND_RESULTS[attempt.outcome]

    def _update(
        self, view: AlertView, attempt: _Attempt, now_ms: int
    ) -> tuple[DeliveryUpdate, DeliveryResult]:
        alert_id = view.alert.id
        attempts = view.alert.discord_attempts + 1
        if attempt.outcome == "sent":
            return DeliveryUpdate(alert_id=alert_id, state="sent", attempts=attempts), "sent"
        if attempt.outcome == "rate_limited":
            # Attempts unchanged: Discord asked to wait, the message was not refused.
            next_at_ms = now_ms + round(attempt.retry_after_s * _MS_PER_S)
            update = DeliveryUpdate(
                alert_id=alert_id,
                state="pending",
                attempts=view.alert.discord_attempts,
                next_at_ms=next_at_ms,
            )
            return update, "rate_limited"
        if attempt.outcome == "refused":
            return DeliveryUpdate(alert_id=alert_id, state="failed", attempts=attempts), "suspended"
        if attempt.outcome == "retry" and attempts < MAX_ATTEMPTS:
            next_at_ms = now_ms + round(_backoff_s(attempts) * _MS_PER_S)
            update = DeliveryUpdate(
                alert_id=alert_id, state="pending", attempts=attempts, next_at_ms=next_at_ms
            )
            return update, "retried"
        return DeliveryUpdate(alert_id=alert_id, state="failed", attempts=attempts), "failed"

    def _log_attempt(self, attempt: _Attempt, alerts: int) -> None:
        what = f"status {attempt.status}" if attempt.status is not None else attempt.error
        if attempt.outcome == "sent":
            _log.info("discord: %d alerts sent", alerts)
        elif attempt.outcome == "rate_limited":
            self._paused_until = self._monotonic() + attempt.retry_after_s
            _log.warning(
                "discord rate limited the webhook; next message in %.0f s", attempt.retry_after_s
            )
        elif attempt.outcome == "refused":
            # Once: no request goes out after this one until the app restarts.
            self._suspended_status = attempt.status
            _log.error(
                "discord refused the webhook (%s); sending suspended until the app"
                " restarts: check CCOBS_DISCORD_WEBHOOK_URL",
                what,
            )
        elif attempt.outcome == "retry":
            _log.warning("discord delivery of %d alerts failed (%s); will retry", alerts, what)
        else:
            _log.error("discord rejected a message of %d alerts (%s); not retried", alerts, what)

    def _count(self, result: DeliveryResult, count: int) -> None:
        if self._on_delivery is None:
            return
        try:
            self._on_delivery(result, count)
        except Exception as error:
            _log.error("discord delivery not counted (%s)", type(error).__name__)

    # HTTP.

    def _post(self, payload: Payload) -> _Attempt:
        """POST ``payload`` to the webhook; never raises, never logs."""
        try:
            with self._client.stream(
                "POST", self._webhook_url.get_secret_value(), params=_WAIT, json=payload
            ) as response:
                return self._answer(response)
        except httpx.TimeoutException:
            return _Attempt(outcome="retry", error="timeout")
        except httpx.ConnectError:
            return _Attempt(outcome="retry", error="connection error")
        except Exception as error:
            # httpx's other errors, whose messages hold the URL: the type only.
            return _Attempt(outcome="retry", error=type(error).__name__)

    def _answer(self, response: httpx.Response) -> _Attempt:
        status = response.status_code
        if response.is_success:
            return _Attempt(outcome="sent", status=status)
        if status == _RATE_LIMITED:
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > _BODY_MAX_BYTES:
                    break
            retry_after_s = _retry_after(response, bytes(body[:_BODY_MAX_BYTES]))
            return _Attempt(outcome="rate_limited", status=status, retry_after_s=retry_after_s)
        if status in REFUSED_STATUSES:
            return _Attempt(outcome="refused", status=status)
        if status >= _SERVER_ERROR:
            return _Attempt(outcome="retry", status=status)
        return _Attempt(outcome="rejected", status=status)

    # The settings screen's test button.

    def send_connection_test(self) -> ConnectionTestResult:
        """Post the test message now, on the caller's thread, unless the last test was
        less than ``TEST_INTERVAL_S`` ago. Stores nothing and never suspends sending."""
        with self._test_lock:
            now = self._monotonic()
            if self._last_test is not None and now - self._last_test < TEST_INTERVAL_S:
                return ConnectionTestResult(kind="too_soon")
            self._last_test = now
        attempt = self._post(connection_test_payload(self.host))
        if attempt.outcome == "sent":
            _log.info("discord test message sent")
            return ConnectionTestResult(kind="sent", status=attempt.status)
        if attempt.status is not None:
            _log.warning("discord did not accept the test message (status %d)", attempt.status)
            return ConnectionTestResult(kind="refused", status=attempt.status)
        reason = attempt.error if attempt.error in ("timeout", "connection error") else "error"
        _log.warning("discord test message not sent (%s)", attempt.error)
        return ConnectionTestResult(kind="unreachable", reason=reason)
