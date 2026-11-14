"""Broker error taxonomy (ARCHITECTURE §10.5, refined in step 9).

Each error class states two facts:

* ``order_state`` — what the error *itself* proves about an order at the broker:
  ``NOT_CREATED`` (authoritative non-acceptance), ``EXISTS``, ``UNKNOWN``, or
  ``NOT_APPLICABLE`` (reads, or errors that prove nothing about an order).
* ``read_retryable`` — whether repeating the same **read** may succeed. It never
  says anything about writes.

Write rule: **no write is ever retried because of the error class it raised.** After a
failed submit, the only authoritative outcomes are an :class:`OrderRejectedError`
carrying the broker's definitive rejection (``NOT_CREATED``) and
:class:`DuplicateClientOrderIdError` (``EXISTS``). Everything else — 429, 5xx, 504,
timeouts, connection resets, auth failures, malformed responses — leaves the order
``UNKNOWN`` (see :func:`submit_failure_state`), and recovery must look the order up by
its stable client_order_id before anything else (§10.3). A resend, if ever allowed,
reuses the same client_order_id.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Final


class OrderState(StrEnum):
    NOT_CREATED = "NOT_CREATED"
    EXISTS = "EXISTS"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class BrokerError(RuntimeError):
    order_state: OrderState = OrderState.NOT_APPLICABLE
    read_retryable: bool = False

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OrderRejectedError(BrokerError):
    """The broker's definitive business rejection of a submit: no order was created.
    Only raise this on authoritative evidence (an explicit rejection response)."""

    order_state = OrderState.NOT_CREATED


class NotCancelableError(BrokerError):
    """Cancel refused because the order is already terminal (or not visible)."""

    order_state = OrderState.EXISTS


class DuplicateClientOrderIdError(BrokerError):
    """The client_order_id is already used: an order EXISTS. Look it up and adopt it;
    never treat this as a rejection of the intent."""

    order_state = OrderState.EXISTS


class AmbiguousSubmissionError(BrokerError):
    """The submit may or may not have been accepted (timeout, connection drop, 5xx, 504,
    429 without proof of non-acceptance). The order state is UNKNOWN."""

    order_state = OrderState.UNKNOWN


class AuthFailureKind(StrEnum):
    """What the response evidence proves about a 401/403 (Step 16.5A)."""

    CREDENTIALS_REJECTED = "CREDENTIALS_REJECTED"
    """HTTP 401: the API key/secret were not accepted (invalid, expired or revoked)."""
    FORBIDDEN_UNRESOLVED = "FORBIDDEN_UNRESOLVED"
    """HTTP 403 without recognized evidence of its cause. Treated as an authentication
    failure (fail closed); the cause is explicitly unknown."""


class AuthenticationError(BrokerError):
    """401, or a 403 not proven to be a market-data entitlement limit. Never retried;
    leads to a persistent halt (AUTH). ``kind`` records what the evidence proves and
    ``evidence`` (403 only) why a 403 stayed unresolved; neither changes handling."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        kind: AuthFailureKind | None = None,
        evidence: str | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.kind = kind if kind is not None else (
            AuthFailureKind.CREDENTIALS_REJECTED if status_code == 401
            else AuthFailureKind.FORBIDDEN_UNRESOLVED
        )  # fmt: skip
        self.evidence = evidence


class DataEntitlementKind(StrEnum):
    """Which market-data restriction a recognized 403 names (Step 16.5A)."""

    RECENT_DATA_WINDOW = "RECENT_DATA_WINDOW"
    """The subscription does not include the feed's most recent data (a time window)."""


class MarketDataEntitlementError(BrokerError):
    """A market-data GET refused by the data host with HTTP 403 whose body names a
    subscription restriction (Step 16.5A). The dependent data operation fails (never
    retried, never substituted); it is **not** evidence that the trading credentials are
    invalid, so it is not an :class:`AuthenticationError` and never an AUTH halt. Only a
    recognized body produces this error; any other 403 stays an AuthenticationError."""

    def __init__(
        self,
        message: str,
        *,
        kind: DataEntitlementKind,
        feed: str | None = None,
        status_code: int | None = 403,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.kind = kind
        self.feed = feed


class RateLimitedError(BrokerError):
    """429 on a READ: the read may be retried under a bounded policy. A 429 on a write
    is reported as AmbiguousSubmissionError instead — it does not prove non-acceptance."""

    read_retryable = True

    def __init__(
        self,
        message: str,
        *,
        retry_after: timedelta | None = None,
        status_code: int | None = 429,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.retry_after = retry_after


class ServiceUnavailableError(BrokerError):
    """A read failed transiently (timeout, connection error, 5xx). Retryable for reads
    only; a transport failure on submit is AmbiguousSubmissionError.

    ``kind`` says which transport condition it was (``timeout``, ``network``,
    ``http`` for a 5xx, or ``unavailable`` when unspecified); the broker-outage circuit
    breaker records it as the failure category."""

    read_retryable = True

    def __init__(
        self, message: str, *, status_code: int | None = None, kind: str = "unavailable"
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.kind = "http" if status_code is not None and status_code >= 500 else kind


class BrokerRequestError(BrokerError):
    """The broker refused a read request as malformed (400/422 on a GET): a bug on our
    side. Not retried."""


class InvalidBrokerDataError(BrokerError):
    """A response could not be mapped exactly (missing field, float where a decimal
    string is required, unknown enum or status, naive timestamp, duplicate id with
    different content, non-advancing pagination, ...). Leads to a persistent halt
    (BROKER_PAYLOAD); never retried blindly."""


class UnknownBrokerStatusError(InvalidBrokerDataError):
    """An order status outside the explicit mapping table (§3 V6): persistent halt
    (UNMAPPED_BROKER_STATUS)."""


class OrderNotFoundError(BrokerError):
    """A lookup found nothing. NOT proof that an order does not exist (§10.3)."""


class IncompleteRetrievalError(BrokerError):
    """A multi-request read could not be proven complete (saturated page at a single
    timestamp, interrupted pagination, page cap reached, unclosed window). Whatever was
    read is attached as ``partial`` (safe to ingest idempotently) but must never be
    reported or recorded as complete."""

    def __init__(self, message: str, *, partial: tuple[object, ...] = ()) -> None:
        super().__init__(message)
        self.partial = partial


class UnsupportedBrokerOperationError(BrokerError):
    """The requested read has no verified source (e.g. official closing prices). Fails
    closed instead of substituting a different kind of data."""


def submit_failure_state(error: BaseException) -> OrderState:
    """What a failed submit proves about the order. Only an explicit broker rejection
    proves non-acceptance; anything unexpected is UNKNOWN."""
    if isinstance(error, OrderRejectedError):
        return OrderState.NOT_CREATED
    if isinstance(error, DuplicateClientOrderIdError):
        return OrderState.EXISTS
    return OrderState.UNKNOWN


PAYLOAD_CAUSE_MAX_DEPTH: Final = 4
"""How many ``IncompleteRetrievalError`` wrappers :func:`payload_cause` will look through."""


def payload_cause(exc: BaseException) -> InvalidBrokerDataError | None:
    """The malformed-payload error behind ``exc``, or ``None``.

    Recognizes ``exc`` itself (``InvalidBrokerDataError``, incl. ``UnknownBrokerStatusError``)
    and the ``__cause__`` of an :class:`IncompleteRetrievalError` — e.g. a page that failed
    to parse inside ``collect_window`` — following at most
    :data:`PAYLOAD_CAUSE_MAX_DEPTH` such wrappers. Nothing else is traversed: a timeout,
    network failure, HTTP 429 or a genuinely incomplete pagination (no cause, or a
    non-payload cause) is never a payload error. The exception and its cause chain are
    not modified; the incomplete-retrieval semantics (partial results are never
    complete) are unaffected.
    """
    current: BaseException | None = exc
    for _ in range(PAYLOAD_CAUSE_MAX_DEPTH + 1):
        if isinstance(current, InvalidBrokerDataError):
            return current
        if not isinstance(current, IncompleteRetrievalError):
            return None
        current = current.__cause__
    return None
