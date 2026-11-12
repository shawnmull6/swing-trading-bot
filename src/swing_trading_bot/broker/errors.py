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


class AuthenticationError(BrokerError):
    """401/403. Never retried; leads to a persistent halt (AUTH)."""


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
    only; a transport failure on submit is AmbiguousSubmissionError."""

    read_retryable = True


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
