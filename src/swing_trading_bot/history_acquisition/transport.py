"""Read-only transport integration for historical acquisition (Step 16.17A, Task 1).

Three layers, outermost first, each refusing on its own:

1. :class:`GuardedHttpSession` wraps the ``requests.Session`` handed to
   :class:`~swing_trading_bot.broker.alpaca_http.AlpacaReadTransport`. It exposes only
   ``get``; every other attribute (``post``, ``request``, ``send``, ``mount``, ...) is
   refused. A GET must name one of the plan's exact (origin, path) pairs, carry only that
   endpoint's planned query keys (plus ``page_token``), set ``allow_redirects=False`` and
   arrive while the session's hard attempt cap and deadline still allow it (at least one
   whole second left); connect and read timeouts are cut to the whole seconds left.
   ``trust_env`` is forced off (no environment proxies, CA bundles or ``.netrc``) and
   explicit proxies are cleared. A redirect status or a response URL on another origin
   or path is refused. Every check that can be made
   before sending is made before sending.
2. ``AlpacaReadTransport`` itself (unchanged): GET only to the two allowlisted HTTPS
   origins, the status taxonomy (401/403 never retried, 403 classified on the data host),
   exact-decimal decoding (``parse_float=Decimal``) and the depth limit.
3. :class:`AcquisitionFetch` — the ``fetch`` callable of
   :func:`~swing_trading_bot.research.staging.run_session`. It accepts only a batch and
   request that are *identical* to the plan's, sends exactly the planned parameters (plus
   the page token being followed), validates the decoded page for its batch kind and
   returns its **canonical serialization** (sorted keys, no whitespace, decimals written
   exactly as received; a float, NaN or Infinity is refused).

**Provenance (Step 16.17A.1).** Two digests are kept apart. The guard hashes each
response body exactly as ``requests`` delivered it (``response.content``: the entity
body after HTTP transfer and content decoding — gzip is undone by the client — and
before any charset or JSON decoding); the adapter returns it with the canonical body as
a :class:`~swing_trading_bot.research.staging.FetchedPage`. The staging record keeps
``received_sha256`` (that digest) beside ``sha256`` (the stored canonical blob); the
received bytes themselves are never stored or logged. Bytes on the wire before content
decoding are not accessible through ``requests`` and are not claimed.

**Retry-After.** On a 429 or 503 the guard parses ``Retry-After`` (delta-seconds, or an
IMF-fixdate HTTP-date measured against the injected wall clock); obsolete date formats,
lists, fractions and negative values are invalid and leave the fallback backoff in
force. The adapter re-raises the error with that ``retry_after`` and a fixed message;
``run_session`` decides whether the wait fits (it never waits past the deadline).

**Stop reasons.** :class:`TransportRefusedError` is a
:class:`~swing_trading_bot.research.staging.FetchRefusedError` whose ``reason`` follows
from its code: request-side refusals are ``UNAUTHORIZED_ENDPOINT``, the guard's cap and
deadline are ``SESSION_ATTEMPT_BUDGET_EXHAUSTED`` / ``SESSION_DEADLINE_EXHAUSTED``,
redirects are ``REDIRECT_REFUSED``; only response-content failures are
``INVALID_PROVIDER_RESPONSE``.

**Deadline after a response (Step 16.17A.3).** Connect and read timeouts bound each
socket wait, not a whole request. When a response (any status) completes at or after the
session deadline the guard refuses it with ``RESPONSE_AFTER_DEADLINE``
(``SESSION_DEADLINE_EXHAUSTED``) before reading its status, ``Retry-After`` or body
digest; the attempt stays counted. ``run_session`` checks again after the adapter's
validation and before committing a batch.

There are no fallback endpoints and no write path. Account, order, position, activity
and account-configuration paths are not in the plan, so the guard refuses them.
Credentials are loaded only by :func:`connect_live`, through the existing
:func:`~swing_trading_bot.app.credentials.load_credentials` (file source only, trading
disabled), and only after every offline check of the runner has passed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal, Protocol, cast
from urllib.parse import urlsplit

from swing_trading_bot.app.credentials import load_credentials
from swing_trading_bot.broker.alpaca_http import (
    ALLOWED_ORIGINS,
    CONNECT_TIMEOUT_S,
    CORPORATE_ACTIONS_PATH,
    READ_TIMEOUT_S,
    AlpacaReadTransport,
)
from swing_trading_bot.broker.errors import (
    InvalidBrokerDataError,
    RateLimitedError,
    ServiceUnavailableError,
)
from swing_trading_bot.broker.mapping import session_from_raw, settlement_days_from_raw
from swing_trading_bot.broker.models import BarAdjustment
from swing_trading_bot.research.acquisition import (
    HOST_ORIGINS,
    AcquisitionBatch,
    AcquisitionPlan,
    BatchKind,
    Host,
    LogicalRequest,
)
from swing_trading_bot.research.corporate_actions import CorporateActionParseError, parse_page
from swing_trading_bot.research.dataset import DatasetFormatError, bars_from_alpaca_pages
from swing_trading_bot.research.staging import FetchedPage, FetchRefusedError, StopReason

#: The only (origin, path) pairs an acquisition session may request. The plan's own
#: endpoints must equal this set exactly (checked when the guard is built).
ACQUISITION_ENDPOINTS: Final = frozenset({
    ("https://paper-api.alpaca.markets", "/v2/calendar"),
    ("https://data.alpaca.markets", "/v2/stocks/bars"),
    ("https://data.alpaca.markets", "/v1/corporate-actions"),
})  # fmt: skip
PAGE_TOKEN: Final = "page_token"  # noqa: S105 - a query key, not a secret
#: Keys of a ``GET /v2/calendar`` row, as captured by the 16.16D probe (both date types).
CALENDAR_ROW_KEYS: Final = frozenset(
    {"date", "open", "close", "session_open", "session_close", "settlement_date"}
)
#: Top-level keys of a ``GET /v2/stocks/bars`` page (16.16D probe).
BARS_PAGE_KEYS: Final = frozenset({"bars", "next_page_token"})
BAR_REQUIRED_KEYS: Final = frozenset({"t", "o", "h", "l", "c", "v"})
BAR_ALLOWED_KEYS: Final = BAR_REQUIRED_KEYS | {"n", "vw"}
_JSON_NUMBER: Final = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$")
_VALIDATION_RECEIPT: Final = datetime(1970, 1, 1, tzinfo=UTC)
"""Receipt time used only to run the bar validation at fetch time (not stored)."""


MAX_RETRY_AFTER: Final = timedelta(days=1)
"""Longer ``Retry-After`` values are clamped here (they exceed any session deadline)."""
_RETRY_DELTA: Final = re.compile(r"^[0-9]{1,12}$")
_WEEKDAYS: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS: Final = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
                  "Nov", "Dec")  # fmt: skip
_IMF_FIXDATE: Final = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), ([0-9]{2}) "
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) ([0-9]{4}) "
    r"([0-9]{2}):([0-9]{2}):([0-9]{2}) GMT$"
)
_REQUEST_SIDE: Final = frozenset({
    "URL_REFUSED", "ENDPOINT_NOT_ALLOWLISTED", "REQUEST_OPTION_REFUSED",
    "REDIRECTS_NOT_DISABLED", "QUERY_REFUSED", "REQUEST_NOT_IN_PLAN", "PLANNED_PAGE_TOKEN",
    "CALENDAR_IS_NOT_PAGINATED", "ENDPOINT_NOT_IN_PLAN", "ORIGIN_ALLOWLIST_MISMATCH",
    "PLAN_ENDPOINTS_NOT_APPROVED", "EMPTY_PAGE_TOKEN_REQUESTED",
})  # fmt: skip
_REASONS: Final[Mapping[str, StopReason]] = {
    "SESSION_ATTEMPT_CAP": StopReason.SESSION_ATTEMPT_BUDGET_EXHAUSTED,
    "SESSION_DEADLINE": StopReason.SESSION_DEADLINE_EXHAUSTED,
    "RESPONSE_AFTER_DEADLINE": StopReason.SESSION_DEADLINE_EXHAUSTED,
    "REDIRECT_REFUSED": StopReason.REDIRECT_REFUSED,
    "UNEXPECTED_RESPONSE_URL": StopReason.REDIRECT_REFUSED,
    "RECEIVED_BODY_UNAVAILABLE": StopReason.PROVENANCE_UNAVAILABLE,
}


def refusal_reason(code: str) -> StopReason:
    """The stop reason of a transport refusal code (response content by default)."""
    if code in _REQUEST_SIDE or code.startswith("ATTRIBUTE_REFUSED_"):
        return StopReason.UNAUTHORIZED_ENDPOINT
    return _REASONS.get(code, StopReason.INVALID_PROVIDER_RESPONSE)


class TransportRefusedError(FetchRefusedError):
    """A request or response outside the acquisition contract. ``code`` is fixed text;
    no URL, body, token or credential is ever part of it. ``reason`` says which kind of
    refusal it is (:func:`refusal_reason`); the batch stages nothing and the session
    stops with that reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code, refusal_reason(code))


def parse_retry_after(value: str | None, now: datetime) -> tuple[timedelta | None, str]:
    """(wait, format) for a ``Retry-After`` header value. Formats: ``ABSENT``,
    ``DELTA_SECONDS``, ``HTTP_DATE`` (IMF-fixdate only, weekday checked, a past date
    waits zero) or ``INVALID`` (anything else, including the obsolete RFC 850/asctime
    dates, lists, fractions, signs, or a wall clock without a timezone). A valid wait is
    clamped to :data:`MAX_RETRY_AFTER`; ``None`` means the fallback backoff applies."""
    if value is None:
        return None, "ABSENT"
    text = value.strip(" \t")
    if _RETRY_DELTA.match(text):
        return min(timedelta(seconds=int(text)), MAX_RETRY_AFTER), "DELTA_SECONDS"
    m = _IMF_FIXDATE.match(text)
    if m is None or now.utcoffset() is None:
        return None, "INVALID"
    wd, day, mon, year, hh, mm, ss = m.groups()
    try:
        when = datetime(int(year), _MONTHS.index(mon) + 1, int(day), int(hh), int(mm),
                        int(ss), tzinfo=UTC)  # fmt: skip
    except ValueError:
        return None, "INVALID"
    if _WEEKDAYS[when.weekday()] != wd:
        return None, "INVALID"
    return min(max(when - now, timedelta(0)), MAX_RETRY_AFTER), "HTTP_DATE"


# ===================================================================== canonical bytes


def _encode(value: object, out: list[str]) -> None:  # noqa: PLR0912 - one flat type switch
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise TransportRefusedError("NON_FINITE_NUMBER")
        text = str(value)
        if not _JSON_NUMBER.match(text) or not any(c in text for c in ".eE"):
            raise TransportRefusedError("NON_CANONICAL_DECIMAL")
        out.append(text)
    elif type(value).__name__ == "float":  # rejected by name: no binary floats anywhere
        raise TransportRefusedError("FLOAT_IN_RESPONSE")
    elif isinstance(value, str):
        out.append(json.dumps(value, ensure_ascii=True))
    elif isinstance(value, list):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _encode(item, out)
        out.append("]")
    elif isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise TransportRefusedError("NON_STRING_KEY")
        out.append("{")
        for i, key in enumerate(sorted(value)):
            if i:
                out.append(",")
            out.append(json.dumps(key, ensure_ascii=True))
            out.append(":")
            _encode(value[key], out)
        out.append("}")
    else:
        raise TransportRefusedError("UNSUPPORTED_JSON_VALUE")


def canonical_body(doc: object) -> bytes:
    """The canonical bytes of a document decoded with ``parse_float=Decimal``: sorted keys,
    no whitespace, ASCII, every decimal written exactly as it was received (so decoding
    the bytes again with ``parse_float=Decimal`` gives an equal document, number types
    and exponents included). Floats, NaN and Infinity are refused."""
    out: list[str] = []
    _encode(doc, out)
    return "".join(out).encode("ascii")


# ===================================================================== guarded session


class HttpSession(Protocol):
    """The part of ``requests.Session`` the guard uses."""

    trust_env: bool

    def get(self, url: str, **kwargs: Any) -> Any: ...  # noqa: ANN401 - requests.Response


@dataclass
class AttemptRecord:
    """One attempt the guard let through (or refused before sending). No query values,
    headers or bodies are kept: the page token is recorded as a short digest and the
    received body only as its SHA-256 and length."""

    path: str
    status: int | None = None
    error: str | None = None
    page_token_sha256: str | None = None
    elapsed_ms: int | None = None
    received_sha256: str | None = None
    received_bytes: int | None = None
    retry_after_format: str | None = None
    retry_after_ms: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "status": self.status, "error": self.error,
                "page_token_sha256": self.page_token_sha256,
                "elapsed_ms": self.elapsed_ms, "received_sha256": self.received_sha256,
                "received_bytes": self.received_bytes,
                "retry_after_format": self.retry_after_format,
                "retry_after_ms": self.retry_after_ms}  # fmt: skip


@dataclass(frozen=True, slots=True)
class ReceivedBody:
    """SHA-256 and length of one 2xx response body as the client received it."""

    sha256: str
    length: int


def _utc_now() -> datetime:  # pragma: no cover - live clock
    return datetime.now(UTC)


def plan_endpoints(plan: AcquisitionPlan) -> dict[tuple[str, str], frozenset[str]]:
    """(origin, path) -> the query keys the plan uses there (plus ``page_token``).
    Refuses a plan whose endpoints are not exactly :data:`ACQUISITION_ENDPOINTS` or whose
    origins differ from the transport's allowlist."""
    if dict(ALLOWED_ORIGINS) != {"trading": HOST_ORIGINS[Host.TRADING],
                                 "data": HOST_ORIGINS[Host.DATA]}:  # fmt: skip
        raise TransportRefusedError("ORIGIN_ALLOWLIST_MISMATCH")
    keys: dict[tuple[str, str], set[str]] = {}
    for req in plan.requests:
        keys.setdefault((HOST_ORIGINS[req.host], req.path), {PAGE_TOKEN}).update(
            k for k, _ in req.params
        )
    if set(keys) != ACQUISITION_ENDPOINTS:
        raise TransportRefusedError("PLAN_ENDPOINTS_NOT_APPROVED")
    return {k: frozenset(v) for k, v in keys.items()}


class GuardedHttpSession:
    """See the module docstring (layer 1). ``remaining`` returns the seconds left in the
    session; ``max_attempts`` is the session's hard cap (a backstop: the staging runner
    counts and refuses first)."""

    def __init__(
        self,
        inner: HttpSession,
        *,
        endpoints: Mapping[tuple[str, str], frozenset[str]],
        max_attempts: int,
        remaining: Callable[[], timedelta],
        monotonic: Callable[[], timedelta] | None = None,
        wall_clock: Callable[[], datetime] = _utc_now,
        connect_timeout_s: int = CONNECT_TIMEOUT_S,
        read_timeout_s: int = READ_TIMEOUT_S,
    ) -> None:
        inner.trust_env = False
        if getattr(inner, "proxies", None):
            inner.proxies = {}  # type: ignore[attr-defined]
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_endpoints", dict(endpoints))
        object.__setattr__(self, "_max_attempts", max_attempts)
        object.__setattr__(self, "_remaining", remaining)
        object.__setattr__(self, "_monotonic", monotonic)
        object.__setattr__(self, "_wall_clock", wall_clock)
        object.__setattr__(self, "_received", None)
        object.__setattr__(self, "_retry_after", None)
        object.__setattr__(self, "_timeouts", (connect_timeout_s, read_timeout_s))
        object.__setattr__(self, "attempts", [])
        object.__setattr__(self, "refusals", [])
        object.__setattr__(self, "_sent", 0)

    # Everything but ``get`` and ``trust_env = False`` is refused.
    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        raise TransportRefusedError(f"ATTRIBUTE_REFUSED_{name.upper()}")

    def __setattr__(self, name: str, value: object) -> None:
        if name == "trust_env" and value is False:
            self._inner.trust_env = False
            return
        raise TransportRefusedError(f"ATTRIBUTE_REFUSED_{name.upper()}")

    @property
    def trust_env(self) -> bool:
        return bool(self._inner.trust_env)

    def pop_received(self) -> ReceivedBody | None:
        """The received-body digest of the last 2xx response, once (then cleared)."""
        got: ReceivedBody | None = self._received
        object.__setattr__(self, "_received", None)
        return got

    def pop_retry_after(self) -> timedelta | None:
        """The parsed ``Retry-After`` of the last 429/503 response, once (then cleared);
        ``None`` when absent or invalid."""
        got: timedelta | None = self._retry_after
        object.__setattr__(self, "_retry_after", None)
        return got

    @property
    def sent(self) -> int:
        """Attempts actually handed to the inner session."""
        return int(self._sent)

    def _refuse(self, code: str, path: str = "") -> TransportRefusedError:
        self.refusals.append(code)
        self.attempts.append(AttemptRecord(path, error=f"REFUSED_{code}"))
        return TransportRefusedError(code)

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: object = None,
        allow_redirects: bool = True,
        **extra: object,
    ) -> Any:  # noqa: ANN401 - requests.Response
        del timeout  # replaced by the guard's own, cut to the time left
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.hostname or ''}"
        if (parts.username or parts.password or parts.port is not None
                or parts.query or parts.fragment):  # fmt: skip
            raise self._refuse("URL_REFUSED")
        allowed_keys = self._endpoints.get((origin, parts.path))
        if allowed_keys is None or parts.netloc != parts.hostname:
            raise self._refuse("ENDPOINT_NOT_ALLOWLISTED")
        if extra:
            raise self._refuse("REQUEST_OPTION_REFUSED", parts.path)
        if allow_redirects is not False:
            raise self._refuse("REDIRECTS_NOT_DISABLED", parts.path)
        query = dict(params or {})
        if not set(query) <= allowed_keys or not all(isinstance(v, str) for v in query.values()):
            raise self._refuse("QUERY_REFUSED", parts.path)
        if self._sent >= self._max_attempts:
            raise self._refuse("SESSION_ATTEMPT_CAP", parts.path)
        left = self._remaining() // timedelta(seconds=1)  # whole seconds, rounded down
        if left < 1:
            raise self._refuse("SESSION_DEADLINE", parts.path)
        token = query.get(PAGE_TOKEN)
        record = AttemptRecord(
            parts.path,
            page_token_sha256=None if token is None
            else hashlib.sha256(token.encode()).hexdigest()[:16],
        )  # fmt: skip
        self.attempts.append(record)
        object.__setattr__(self, "_sent", self._sent + 1)
        object.__setattr__(self, "_received", None)
        object.__setattr__(self, "_retry_after", None)
        connect, read = self._timeouts
        started = self._monotonic() if self._monotonic else None
        try:
            response = self._inner.get(
                url, params=query, headers=dict(headers or {}),
                timeout=(min(connect, left), min(read, left)), allow_redirects=False,
            )  # fmt: skip
        except Exception as exc:
            record.error = type(exc).__name__
            raise
        finally:
            if started is not None and self._monotonic is not None:
                record.elapsed_ms = (self._monotonic() - started) // timedelta(milliseconds=1)
        if self._remaining() <= timedelta(0):  # timeouts bound each read, not the request
            record.status = int(response.status_code)
            record.error = "REFUSED_RESPONSE_AFTER_DEADLINE"
            self.refusals.append("RESPONSE_AFTER_DEADLINE")
            raise TransportRefusedError("RESPONSE_AFTER_DEADLINE")
        self._inspect(record, response, origin, parts.path)
        return response

    def _inspect(self, record: AttemptRecord, response: Any, origin: str,  # noqa: ANN401
                 path: str) -> None:  # fmt: skip
        """Status, Retry-After, redirect/URL refusal and the received-body digest."""
        record.status = int(response.status_code)
        if record.status in (429, 503):
            headers = getattr(response, "headers", None) or {}
            wait, fmt = parse_retry_after(headers.get("Retry-After"), self._wall_clock())
            record.retry_after_format = fmt
            if wait is not None:
                record.retry_after_ms = wait // timedelta(milliseconds=1)
            object.__setattr__(self, "_retry_after", wait)
        if 300 <= record.status < 400:
            self.refusals.append("REDIRECT_REFUSED")
            raise TransportRefusedError("REDIRECT_REFUSED")
        final = getattr(response, "url", None)
        if final:
            got = urlsplit(str(final))
            if f"{got.scheme}://{got.netloc}" != origin or got.path != path:
                self.refusals.append("UNEXPECTED_RESPONSE_URL")
                raise TransportRefusedError("UNEXPECTED_RESPONSE_URL")
        content = getattr(response, "content", None)
        if 200 <= record.status < 300 and isinstance(content, bytes):
            record.received_sha256 = hashlib.sha256(content).hexdigest()
            record.received_bytes = len(content)
            object.__setattr__(self, "_received",
                               ReceivedBody(record.received_sha256, len(content)))  # fmt: skip

    def summary(self) -> dict[str, object]:
        statuses: dict[str, int] = {}
        for a in self.attempts:
            key = str(a.status) if a.status is not None else str(a.error)
            statuses[key] = statuses.get(key, 0) + 1
        return {"attempts_sent": self.sent,
                "outcomes": dict(sorted(statuses.items())),
                "refusals": list(self.refusals),
                "log": [a.to_dict() for a in self.attempts]}  # fmt: skip


# ===================================================================== fetch adapter


class ReadOnlyJson(Protocol):
    """The two read methods of ``AlpacaReadTransport`` the adapter may call."""

    def get_json(self, service: Literal["trading", "data"], path: str,
                 params: Mapping[str, str]) -> object: ...  # fmt: skip

    def get_corporate_actions_json(self, params: Mapping[str, str]) -> object: ...


def _check_calendar(doc: object, date_type: str) -> None:
    if not isinstance(doc, list):
        raise TransportRefusedError("CALENDAR_NOT_A_LIST")
    for row in doc:
        if not isinstance(row, dict) or set(row) != CALENDAR_ROW_KEYS:
            raise TransportRefusedError("CALENDAR_ROW_SHAPE")
        if not all(isinstance(v, str) for v in row.values()):
            raise TransportRefusedError("CALENDAR_ROW_SHAPE")
    try:
        if date_type == "TRADING":
            for row in doc:
                session_from_raw(row)
        else:
            settlement_days_from_raw(doc)
    except InvalidBrokerDataError:
        raise TransportRefusedError("CALENDAR_ROW_INVALID") from None


def _check_bars(doc: object, batch: AcquisitionBatch, params: Mapping[str, str]) -> None:
    if not isinstance(doc, dict) or set(doc) != BARS_PAGE_KEYS:
        raise TransportRefusedError("BARS_PAGE_SHAPE")
    token = doc["next_page_token"]
    if token is not None and (not isinstance(token, str) or not token):
        raise TransportRefusedError("INVALID_PAGE_TOKEN")
    bars = doc["bars"]
    if bars is None:
        return
    source = params["symbols"]
    if not isinstance(bars, dict) or not set(bars) <= {source}:
        raise TransportRefusedError("BARS_UNEXPECTED_SYMBOL")
    for rows in bars.values():
        if not isinstance(rows, list):
            raise TransportRefusedError("BARS_PAGE_SHAPE")
        for row in rows:
            if (not isinstance(row, dict) or not set(row) >= BAR_REQUIRED_KEYS
                    or not set(row) <= BAR_ALLOWED_KEYS):  # fmt: skip
                raise TransportRefusedError("BAR_ROW_SHAPE")
    if params["adjustment"] not in ("raw", "split") or params.get("feed") != "sip":
        raise TransportRefusedError("BARS_PARAMS_UNEXPECTED")
    adjustment: BarAdjustment = "raw" if params["adjustment"] == "raw" else "split"
    try:
        bars_from_alpaca_pages([doc], symbols=[source], start=batch.request_start,
                               end=batch.request_end, feed="sip", adjustment=adjustment,
                               received_at=_VALIDATION_RECEIPT)  # fmt: skip
    except DatasetFormatError:
        raise TransportRefusedError("BAR_ROW_INVALID") from None


def _check_corporate_actions(body: bytes, batch: AcquisitionBatch) -> None:
    try:
        parse_page(body, batch.symbols)
    except CorporateActionParseError as exc:
        raise TransportRefusedError(f"CORPORATE_ACTIONS_{exc.code}") from None


@dataclass
class AcquisitionFetch:
    """The ``fetch`` of :func:`~swing_trading_bot.research.staging.run_session` over the
    read-only transport (see the module docstring, layer 3)."""

    plan: AcquisitionPlan
    transport: ReadOnlyJson
    guard: GuardedHttpSession | None = None
    """The guard under ``transport``: the source of received-body digests and parsed
    ``Retry-After`` values. With a guard, a page without a received digest is refused
    (``RECEIVED_BODY_UNAVAILABLE``); without one, pages carry no received digest."""
    pages_served: int = 0
    refusals: list[str] = field(default_factory=list)
    """Codes of every adapter-level refusal (shape, plan or value checks), in order."""
    _batches: dict[str, AcquisitionBatch] = field(init=False, repr=False)
    _requests: dict[str, LogicalRequest] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        plan_endpoints(self.plan)  # the plan itself must use only approved endpoints
        self._batches = {b.batch_id: b for b in self.plan.batches}
        self._requests = {r.request_id: r for r in self.plan.requests}

    def __call__(self, batch: AcquisitionBatch, req: LogicalRequest,
                 page_token: str | None) -> FetchedPage:  # fmt: skip
        try:
            return self._fetch(batch, req, page_token)
        except TransportRefusedError as exc:
            self.refusals.append(exc.code)
            raise

    def _fetch(self, batch: AcquisitionBatch, req: LogicalRequest,
               page_token: str | None) -> FetchedPage:  # fmt: skip
        if (self._batches.get(batch.batch_id) != batch
                or self._requests.get(req.request_id) != req
                or batch.request_id != req.request_id):  # fmt: skip
            raise TransportRefusedError("REQUEST_NOT_IN_PLAN")
        params = dict(req.params)
        if PAGE_TOKEN in params:
            raise TransportRefusedError("PLANNED_PAGE_TOKEN")
        if page_token is not None:
            if not page_token:
                raise TransportRefusedError("EMPTY_PAGE_TOKEN_REQUESTED")
            if batch.kind is BatchKind.CALENDAR:
                raise TransportRefusedError("CALENDAR_IS_NOT_PAGINATED")
            params[PAGE_TOKEN] = page_token
        doc, received = self._received_get(req, params)
        body = canonical_body(doc)
        if batch.kind is BatchKind.CALENDAR:
            _check_calendar(doc, params["date_type"])
        elif batch.kind is BatchKind.BARS:
            _check_bars(doc, batch, params)
        else:
            _check_corporate_actions(body, batch)
        self.pages_served += 1
        if received is None:
            return FetchedPage(body)
        return FetchedPage(body, received.sha256, received.length)

    def _received_get(self, req: LogicalRequest,
                      params: Mapping[str, str]) -> tuple[object, ReceivedBody | None]:  # fmt: skip
        """GET through the transport; returns the decoded document and the guard's
        received-body digest. 429/503 are re-raised with the guard's ``Retry-After`` and
        a fixed message (the transport's message may quote the body)."""
        guard = self.guard
        if guard is not None:
            guard.pop_received()
            guard.pop_retry_after()
        try:
            doc = self._get(req, params)
        except RateLimitedError:
            wait = guard.pop_retry_after() if guard is not None else None
            raise RateLimitedError("HTTP 429", retry_after=wait) from None
        except ServiceUnavailableError as exc:
            if exc.status_code != 503:
                raise
            wait = guard.pop_retry_after() if guard is not None else None
            raise RetryAfterServiceUnavailableError(wait) from None
        if guard is None:
            return doc, None
        received = guard.pop_received()
        if received is None:
            raise TransportRefusedError("RECEIVED_BODY_UNAVAILABLE")
        return doc, received

    def _get(self, req: LogicalRequest, params: Mapping[str, str]) -> object:
        if req.host is Host.DATA and req.path == CORPORATE_ACTIONS_PATH:
            return self.transport.get_corporate_actions_json(params)
        if req.path in ("/v2/calendar", "/v2/stocks/bars"):
            service: Literal["trading", "data"] = "trading" if req.host is Host.TRADING else "data"
            return self.transport.get_json(service, req.path.removeprefix("/v2"), params)
        raise TransportRefusedError("ENDPOINT_NOT_IN_PLAN")  # pragma: no cover - plan_endpoints


class RetryAfterServiceUnavailableError(ServiceUnavailableError):
    """HTTP 503 with the guard's parsed ``Retry-After`` (``None`` when absent/invalid)."""

    def __init__(self, retry_after: timedelta | None) -> None:
        super().__init__("HTTP 503", status_code=503)
        self.retry_after = retry_after


def transport_summary(fetch: AcquisitionFetch,
                      guarded: GuardedHttpSession) -> dict[str, object]:  # fmt: skip
    """The session report's transport section: attempts, outcomes and every refusal
    (guard first, then adapter), with no query value, header or body."""
    out = guarded.summary()
    out["refusals"] = [*guarded.refusals, *fetch.refusals]
    out["pages_served"] = fetch.pages_served
    return out


# ===================================================================== live wiring


class LiveConnectionRefusedError(RuntimeError):
    """Credentials could not be used for an acquisition session (fixed ``code``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _default_http_session() -> HttpSession:  # pragma: no cover - live only
    import requests  # noqa: PLC0415 - only a live session needs it

    return cast("HttpSession", requests.Session())


def connect_live(
    plan: AcquisitionPlan,
    environ: Mapping[str, str],
    *,
    remaining: Callable[[], timedelta],
    monotonic: Callable[[], timedelta] | None = None,
    wall_clock: Callable[[], datetime] = _utc_now,
    http_session_factory: Callable[[], HttpSession] = _default_http_session,
) -> tuple[AcquisitionFetch, GuardedHttpSession]:
    """Build the live ``fetch``. Credentials come only from an explicitly selected file
    (``STB_ENV_FILE``; a conflicting environment is refused by the loader before the file
    is read), trading must be disabled, and the mode is paper (``Settings`` enforces)."""
    from swing_trading_bot.config.settings import SettingsError  # noqa: PLC0415

    try:
        loaded = load_credentials(environ)
    except SettingsError:  # CredentialSourceError included; messages carry no values
        raise LiveConnectionRefusedError("CREDENTIALS_UNAVAILABLE") from None
    if loaded.source != "file":
        raise LiveConnectionRefusedError("CREDENTIAL_SOURCE_NOT_FILE")
    if loaded.settings.trading_enabled:
        raise LiveConnectionRefusedError("TRADING_ENABLED_REFUSED")
    guarded = GuardedHttpSession(
        http_session_factory(), endpoints=plan_endpoints(plan),
        max_attempts=plan.session_max_attempts, remaining=remaining, monotonic=monotonic,
        wall_clock=wall_clock,
    )  # fmt: skip
    transport = AlpacaReadTransport(loaded.settings, session=guarded)  # type: ignore[arg-type]
    return AcquisitionFetch(plan, transport, guard=guarded), guarded


__all__ = [
    "ACQUISITION_ENDPOINTS",
    "MAX_RETRY_AFTER",
    "AcquisitionFetch",
    "AttemptRecord",
    "GuardedHttpSession",
    "LiveConnectionRefusedError",
    "ReceivedBody",
    "RetryAfterServiceUnavailableError",
    "TransportRefusedError",
    "canonical_body",
    "connect_live",
    "parse_retry_after",
    "plan_endpoints",
    "refusal_reason",
    "transport_summary",
]
