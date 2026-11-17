"""Versioned, checksummed offline research dataset (Step 16.15, Phases 2-3).

**Layout** (one immutable directory per dataset version)::

    <root>/manifest.json                  schema, provenance, universe, file checksums
    <root>/calendar.json                  sessions with open/close and settlement dates
    <root>/identity.json                  ticker history per research symbol
    <root>/bars/<SYMBOL>.raw.jsonl        raw SIP daily bars (decision and execution data)
    <root>/bars/<SYMBOL>.split.jsonl      optional provider split-adjusted bars, used ONLY
                                          to cross-check the split record
    <root>/corporate_actions/<SYMBOL>.json splits, cash distributions, other events and the
                                          attested coverage of each list

Every file is canonical JSON (sorted keys, no insignificant whitespace; one object per
line for ``.jsonl``). Prices and amounts are **decimal strings** — a JSON float, NaN or
Infinity anywhere fails the load. Volumes and counts are JSON integers.

**Integrity.** ``manifest.json`` lists every other file with its SHA-256 and size and
carries ``manifest_sha256`` over its own canonical form without that key. The loader
refuses a dataset with a changed, missing or unlisted file, a changed manifest, or an
unknown schema version (:class:`DatasetIntegrityError`). The manifest digest is the
dataset's identity (``dataset_digest``) and enters every downstream fingerprint.
:func:`write_dataset` stages the whole directory next to its destination, fsyncs it,
then publishes it with one rename; it never overwrites an existing dataset and leaves the
published files read-only.

**Provenance.** The manifest records each acquisition batch (provider, endpoint,
parameters, request and receipt times, request count). Every bar row names its batch and
the ticker it was requested under (``source_symbol``), and the calendar names its batch.

**Corporate-action completeness.** Each list (splits, cash distributions, other events)
has its own coverage record ``{start, end, source, method}``. An empty list means "none
occurred" only inside an attested window whose ``method`` the
:class:`CoverageAcceptance` policy accepts; otherwise the coverage is treated as absent
and the symbol is rejected (``prepare_series`` fails closed, A§12.46). By default a list
the provider merely reported (``PROVIDER_REPORTED``) is **not** accepted: the method must
be a cross-check, an independent source or a recorded operator attestation. When
provider split-adjusted bars are present, the derived split events must equal the
recorded splits exactly (``SPLIT_DERIVATION_MISMATCH``). Price-affecting events the
backtest cannot model (mergers, spin-offs, stock dividends, rights, delistings) inside a
symbol's bar span reject the symbol; a name change must match the identity file.

**Point in time.** Only raw bars are decision inputs; adjusted bars are never returned to
the strategy. Cash in lieu reported by the provider is kept for audit; the backtest
values fractional shares at the prior raw close (A§12.45).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from swing_trading_bot.broker.errors import InvalidBrokerDataError
from swing_trading_bot.broker.mapping import collect_daily_bars
from swing_trading_bot.broker.models import BarAdjustment, BarFeed, DailyBar
from swing_trading_bot.domain.calendar import MARKET_TZ, TradingSession
from swing_trading_bot.strategy.market_data import (
    FATAL_ISSUES,
    CashDividend,
    CorporateActions,
    CoverageWindow,
    DataContractError,
    DividendPolicy,
    SessionCalendar,
    SplitEvent,
    derive_split_events,
    prepare_series,
)
from swing_trading_bot.strategy.walk_forward import (
    RESEARCH_READY,
    ResearchAdmission,
    ResearchInputs,
)

SCHEMA_VERSION: Final = 2
MANIFEST: Final = "manifest.json"
CALENDAR: Final = "calendar.json"
IDENTITY: Final = "identity.json"
MARKET_TIMEZONE: Final = "America/New_York"
_DECIMAL_RE: Final = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")
_SYMBOL_RE: Final = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")


class DatasetError(ValueError):
    """Base class for research-dataset failures."""


class DatasetIntegrityError(DatasetError):
    """Checksums, file set, manifest or schema version do not match."""


class DatasetNotResearchReadyError(DatasetError):
    """The dataset is not RESEARCH_READY; it must not reach walk-forward evaluation or a
    holdout release. ``readiness`` keeps every blocker for inspection."""

    def __init__(self, readiness: DatasetReadiness) -> None:
        super().__init__(
            f"{readiness.state.value}: "
            + "; ".join(readiness.acquisition_blockers + readiness.research_blockers)
        )
        self.readiness = readiness


class DatasetFormatError(DatasetError):
    """A file violates the dataset schema."""


# ===================================================================== JSON


def _reject_float(text: str) -> Any:  # noqa: ANN401 - json hook
    raise DatasetFormatError(f"JSON float {text!r}: prices must be decimal strings")


def _reject_constant(text: str) -> Any:  # noqa: ANN401 - json hook
    raise DatasetFormatError(f"JSON constant {text!r} is not allowed")


def loads(blob: bytes | str) -> Any:  # noqa: ANN401 - decoded JSON
    """Strict JSON: no floats, NaN or Infinity."""
    try:
        return json.loads(blob, parse_float=_reject_float, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise DatasetFormatError(f"invalid JSON: {exc.msg}") from None


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _dec(value: object, name: str) -> Decimal:
    if not isinstance(value, str) or not _DECIMAL_RE.match(value):
        raise DatasetFormatError(f"{name} must be a decimal string, got {value!r}")
    return Decimal(value)


def _int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DatasetFormatError(f"{name} must be a non-negative integer")
    return value


def _day(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise DatasetFormatError(f"{name} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise DatasetFormatError(f"{name}: invalid date {value!r}") from None


def _opt_day(value: object, name: str) -> date | None:
    return None if value is None else _day(value, name)


def _ts(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise DatasetFormatError(f"{name} must be an RFC 3339 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise DatasetFormatError(f"{name}: invalid timestamp {value!r}") from None
    if parsed.tzinfo is None:
        raise DatasetFormatError(f"{name} must carry a UTC offset")
    return parsed


def _str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DatasetFormatError(f"{name} must be a non-empty string")
    return value


def _obj(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DatasetFormatError(f"{name} must be an object")
    return value


def _list(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise DatasetFormatError(f"{name} must be a list")
    return value


# ===================================================================== records


class EvidenceKind(StrEnum):
    """What supports a completeness claim. Only the last two can establish it."""

    PROVIDER_REPORTED = "PROVIDER_REPORTED"
    """The data provider returned a fully paginated list. The provider's own claim."""
    SAME_PROVIDER_CONSISTENCY = "SAME_PROVIDER_CONSISTENCY"
    """An internal consistency check on one provider's data (e.g. its raw vs its
    split-adjusted bars). Corroborates; **not** independent verification."""
    INDEPENDENT_SOURCE = "INDEPENDENT_SOURCE"
    """Corroborated by a different authoritative source (exchange, issuer, vendor)."""
    OPERATOR_REVIEW = "OPERATOR_REVIEW"
    """A recorded human review of named evidence."""


#: Evidence that can never establish completeness on its own, whatever the policy says.
NEVER_SUFFICIENT: Final = frozenset({EvidenceKind.SAME_PROVIDER_CONSISTENCY})
_ATTESTING: Final = frozenset({EvidenceKind.INDEPENDENT_SOURCE, EvidenceKind.OPERATOR_REVIEW})


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: EvidenceKind
    source: str
    """Who or what produced it (provider endpoint, independent source, reviewer)."""
    reference: str
    """Where it can be re-examined (batch id, document, review record)."""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EvidenceKind):
            raise DatasetFormatError("evidence kind must be an EvidenceKind")
        if not self.source.strip() or not self.reference.strip():
            raise DatasetFormatError("evidence needs a source and a reference")


@dataclass(frozen=True, slots=True)
class CoverageAcceptance:
    """Which evidence kinds establish completeness, per list. A claim is accepted when
    at least one of its evidence items is of an accepted kind. Default: an independent
    source or an operator review. ``PROVIDER_REPORTED`` may be accepted only by an
    explicit, labelled choice; ``SAME_PROVIDER_CONSISTENCY`` never."""

    splits: frozenset[EvidenceKind] = _ATTESTING
    dividends: frozenset[EvidenceKind] = _ATTESTING
    other_events: frozenset[EvidenceKind] = _ATTESTING

    def __post_init__(self) -> None:
        for name in ("splits", "dividends", "other_events"):
            kinds = getattr(self, name)
            if not kinds or kinds & NEVER_SUFFICIENT:
                raise DatasetError(f"{name}: same-provider consistency cannot attest coverage")

    @property
    def provider_only_allowed(self) -> bool:
        return any(EvidenceKind.PROVIDER_REPORTED in getattr(self, n)
                   for n in ("splits", "dividends", "other_events"))  # fmt: skip


class DistributionKind(StrEnum):
    """Cash paid per share to holders at the close before the ex-date. All are booked as
    cash; tax character is not modelled."""

    CASH_DIVIDEND = "CASH_DIVIDEND"
    SPECIAL_CASH_DIVIDEND = "SPECIAL_CASH_DIVIDEND"
    CAPITAL_GAIN_DISTRIBUTION = "CAPITAL_GAIN_DISTRIBUTION"
    RETURN_OF_CAPITAL = "RETURN_OF_CAPITAL"


class OtherEventKind(StrEnum):
    NAME_CHANGE = "NAME_CHANGE"
    MERGER = "MERGER"
    SPIN_OFF = "SPIN_OFF"
    STOCK_DIVIDEND = "STOCK_DIVIDEND"
    RIGHTS_DISTRIBUTION = "RIGHTS_DISTRIBUTION"
    DELISTING = "DELISTING"
    OTHER = "OTHER"


#: Events the backtest cannot model: a bar span containing one rejects the symbol.
UNSUPPORTED_EVENTS: Final = frozenset(set(OtherEventKind) - {OtherEventKind.NAME_CHANGE})


@dataclass(frozen=True, slots=True)
class Coverage:
    """A completeness claim for one list over ``[start, end]`` and its evidence."""

    start: date
    end: date
    evidence: tuple[Evidence, ...]

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise DatasetFormatError("coverage end precedes start")
        if not self.evidence:
            raise DatasetFormatError("a coverage claim needs evidence")

    @property
    def kinds(self) -> frozenset[EvidenceKind]:
        return frozenset(e.kind for e in self.evidence)

    def accepted_by(self, accepted: frozenset[EvidenceKind]) -> bool:
        return bool(self.kinds & accepted)

    def window(self) -> CoverageWindow:
        label = "+".join(sorted(k.value for k in self.kinds))
        return CoverageWindow(self.start, self.end, f"attested:{label}")

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


@dataclass(frozen=True, slots=True)
class OtherEvent:
    kind: OtherEventKind
    effective_session: date
    detail: str
    source: str


@dataclass(frozen=True, slots=True)
class TickerSpan:
    ticker: str
    start: date
    end: date | None

    def contains(self, d: date) -> bool:
        return self.start <= d and (self.end is None or d <= self.end)


@dataclass(frozen=True, slots=True)
class SymbolIdentity:
    symbol: str
    tickers: tuple[TickerSpan, ...]
    listing_start: date | None
    delisted_on: date | None

    def ticker_on(self, d: date) -> str | None:
        for span in self.tickers:
            if span.contains(d):
                return span.ticker
        return None


@dataclass(frozen=True, slots=True)
class CorporateActionRecord:
    """The file's content, before the acceptance policy is applied."""

    symbol: str
    splits: tuple[SplitEvent, ...]
    cash_in_lieu: tuple[tuple[date, Decimal], ...]
    dividends: tuple[CashDividend, ...]
    distribution_kinds: tuple[tuple[date, DistributionKind], ...]
    other_events: tuple[OtherEvent, ...]
    split_coverage: Coverage | None
    dividend_coverage: Coverage | None
    other_coverage: Coverage | None


# ===================================================================== encoding


def encode_bar(bar: DailyBar, *, source_symbol: str, batch_id: str) -> dict[str, object]:
    return {
        "symbol": bar.symbol, "source_symbol": source_symbol, "session": str(bar.session_date),
        "t": bar.as_of.isoformat(), "o": str(bar.open), "h": str(bar.high), "l": str(bar.low),
        "c": str(bar.close), "v": bar.volume,
        "vw": None if bar.vwap is None else str(bar.vwap), "n": bar.trade_count,
        "feed": bar.feed, "adjustment": bar.adjustment,
        "received_at": bar.received_at.isoformat(), "batch_id": batch_id,
    }  # fmt: skip


def encode_jsonl(rows: Sequence[Mapping[str, object]]) -> bytes:
    return "".join(canonical(r) + "\n" for r in rows).encode()


def encode_json(obj: object) -> bytes:
    return (canonical(obj) + "\n").encode()


def encode_calendar(sessions: Sequence[TradingSession], *, start: date, end: date, source: str,
                    batch_id: str) -> bytes:  # fmt: skip
    rows = []
    for s in sessions:
        if s.settlement_date is None:
            raise DatasetFormatError(f"{s.session_date}: settlement date required")
        rows.append({"date": str(s.session_date), "open": s.open_local.strftime("%H:%M"),
                     "close": s.close_local.strftime("%H:%M"),
                     "settlement_date": str(s.settlement_date)})  # fmt: skip
    return encode_json({"start": str(start), "end": str(end), "source": source,
                        "batch_id": batch_id, "timezone": MARKET_TIMEZONE,
                        "sessions": rows})  # fmt: skip


def _coverage_json(c: Coverage | None) -> dict[str, object] | None:
    if c is None:
        return None
    return {"start": str(c.start), "end": str(c.end),
            "evidence": [{"kind": e.kind.value, "source": e.source, "reference": e.reference}
                         for e in c.evidence]}  # fmt: skip


def encode_corporate_actions(rec: CorporateActionRecord) -> bytes:
    lieu = dict(rec.cash_in_lieu)
    kinds = dict(rec.distribution_kinds)
    return encode_json({
        "symbol": rec.symbol,
        "splits": [{"effective_session": str(s.effective_session), "new_shares": s.new_shares,
                    "old_shares": s.old_shares, "source": s.source,
                    "cash_in_lieu_price": (str(lieu[s.effective_session])
                                           if s.effective_session in lieu else None)}
                   for s in rec.splits],
        "dividends": [{"ex_session": str(d.ex_session), "pay_date": str(d.pay_date),
                       "amount": str(d.amount), "source": d.source,
                       "kind": kinds.get(d.ex_session, DistributionKind.CASH_DIVIDEND).value}
                      for d in rec.dividends],
        "other_events": [{"kind": e.kind.value, "effective_session": str(e.effective_session),
                          "detail": e.detail, "source": e.source} for e in rec.other_events],
        "split_coverage": _coverage_json(rec.split_coverage),
        "dividend_coverage": _coverage_json(rec.dividend_coverage),
        "other_coverage": _coverage_json(rec.other_coverage),
    })  # fmt: skip


def encode_identity(identities: Sequence[SymbolIdentity]) -> bytes:
    return encode_json({"symbols": {
        i.symbol: {"tickers": [{"ticker": t.ticker, "start": str(t.start),
                                "end": None if t.end is None else str(t.end)}
                               for t in i.tickers],
                   "listing_start": None if i.listing_start is None else str(i.listing_start),
                   "delisted_on": None if i.delisted_on is None else str(i.delisted_on)}
        for i in identities}})  # fmt: skip


def bars_from_alpaca_pages(
    pages: Sequence[Mapping[str, Any]],
    *,
    symbols: Sequence[str],
    start: date,
    end: date,
    feed: BarFeed,
    adjustment: BarAdjustment,
    received_at: datetime,
) -> dict[str, tuple[DailyBar, ...]]:
    """Captured ``GET /v2/stocks/bars`` response bodies (decoded with
    ``parse_float=Decimal``) mapped through the same validation as the live adapter
    (:func:`~swing_trading_bot.broker.mapping.collect_daily_bars`). Any fault raises."""
    rows: list[tuple[str, Any, datetime]] = []
    for i, page in enumerate(pages):
        bars = page.get("bars")
        if bars is None:
            continue
        if not isinstance(bars, dict):
            raise DatasetFormatError(f"page {i}: 'bars' must be an object")
        for sym in sorted(bars):
            for raw in _list(bars[sym], f"page {i} {sym}"):
                rows.append((sym, raw, received_at))
    try:
        return collect_daily_bars(rows, symbols, start, end, feed=feed, adjustment=adjustment)
    except InvalidBrokerDataError as exc:
        raise DatasetFormatError(str(exc)) from None


# ===================================================================== writer


def _manifest_digest(manifest: Mapping[str, object]) -> str:
    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    return sha256(canonical(body).encode())


_COVERAGE_KEYS: Final = (("splits", "split_coverage"), ("dividends", "dividend_coverage"),
                         ("other_events", "other_coverage"))  # fmt: skip
_LEDGER_PREFIX: Final = "ledger."


def corporate_action_claims(files: Mapping[str, bytes]) -> list[dict[str, object]]:
    """One row per (symbol, list): the claimed coverage and the evidence that supports it
    (``None`` coverage = no claim). Derived from the corporate-action files; recorded in
    the manifest and re-derived on load."""
    rows: list[dict[str, object]] = []
    for rel in sorted(files):
        if not (rel.startswith("corporate_actions/") and rel.endswith(".json")):
            continue
        doc = _obj(loads(files[rel]), rel)
        for name, key in _COVERAGE_KEYS:
            cov = doc.get(key)
            rows.append({"symbol": doc.get("symbol"), "list": name,
                         "claim": None if cov is None else {
                             "start": cov.get("start"), "end": cov.get("end"),
                             "evidence": cov.get("evidence")}})  # fmt: skip
    return rows


def refuse_ledger_location(root: Path) -> None:
    """Never write or read a dataset (or a staging store) in a directory that holds a
    ledger (the check lists names only; it never opens a ledger file)."""
    for d in (root, root.parent):
        if d.is_dir() and any(c.name.startswith(_LEDGER_PREFIX) for c in d.iterdir()):
            raise DatasetError(f"{d} holds a ledger: datasets live in their own directory")


def write_dataset(root: Path, meta: Mapping[str, object], files: Mapping[str, bytes]) -> str:
    """Publish a new dataset at ``root`` (which must not exist). ``meta`` holds every
    manifest field except ``files`` and ``manifest_sha256``. Returns the dataset digest."""
    if root.exists():
        raise DatasetError(f"{root} exists: datasets are immutable")
    refuse_ledger_location(root)
    if MANIFEST in files or {"files", "manifest_sha256", "corporate_action_claims"} & set(meta):
        raise DatasetError("the manifest is generated, not supplied")
    for rel in files:
        p = Path(rel)
        if p.is_absolute() or ".." in p.parts or rel != p.as_posix():
            raise DatasetError(f"invalid dataset path {rel!r}")
    manifest: dict[str, object] = dict(meta)
    manifest["schema_version"] = SCHEMA_VERSION
    manifest["corporate_action_claims"] = corporate_action_claims(files)
    manifest["files"] = {rel: {"sha256": sha256(blob), "bytes": len(blob)}
                         for rel, blob in sorted(files.items())}  # fmt: skip
    manifest["manifest_sha256"] = _manifest_digest(manifest)
    staging = root.parent / f".{root.name}.staging"
    if staging.exists():
        raise DatasetError(f"{staging} exists: an earlier write was interrupted")
    try:
        for rel, blob in [*sorted(files.items()), (MANIFEST, encode_json(manifest))]:
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
            target.chmod(0o444)
        staging.rename(root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return str(manifest["manifest_sha256"])


# ===================================================================== loader


class QualityStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    ACCEPTED_WITH_WARNINGS = "ACCEPTED_WITH_WARNINGS"
    REJECTED = "REJECTED"


class QualityIssue(StrEnum):
    # fatal (the symbol is rejected)
    BARS_MISSING = "BARS_MISSING"
    BAR_ROW_INVALID = "BAR_ROW_INVALID"
    BAR_FEED_MISMATCH = "BAR_FEED_MISMATCH"
    IDENTITY_MISSING = "IDENTITY_MISSING"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    UNKNOWN_BATCH = "UNKNOWN_BATCH"
    CORPORATE_ACTIONS_MISSING = "CORPORATE_ACTIONS_MISSING"
    CORPORATE_ACTIONS_INVALID = "CORPORATE_ACTIONS_INVALID"
    COVERAGE_EVIDENCE_INSUFFICIENT = "COVERAGE_EVIDENCE_INSUFFICIENT"
    OTHER_EVENT_COVERAGE_MISSING = "OTHER_EVENT_COVERAGE_MISSING"
    OTHER_EVENT_OUTSIDE_COVERAGE = "OTHER_EVENT_OUTSIDE_COVERAGE"
    UNSUPPORTED_CORPORATE_ACTION = "UNSUPPORTED_CORPORATE_ACTION"
    SPLIT_DERIVATION_MISMATCH = "SPLIT_DERIVATION_MISMATCH"
    EVIDENCE_UNVERIFIABLE = "EVIDENCE_UNVERIFIABLE"
    """A claim cites same-provider consistency the dataset cannot re-run."""
    SERIES_REJECTED = "SERIES_REJECTED"
    # warnings (kept; windows spanning a gap produce no signal)
    MISSING_SESSIONS = "MISSING_SESSIONS"
    HISTORY_STARTS_LATE = "HISTORY_STARTS_LATE"
    HISTORY_ENDS_EARLY = "HISTORY_ENDS_EARLY"
    ZERO_VOLUME_SESSIONS = "ZERO_VOLUME_SESSIONS"
    PRICE_DISCONTINUITY_UNEXPLAINED = "PRICE_DISCONTINUITY_UNEXPLAINED"
    SPLIT_CONSISTENCY_CHECK_UNAVAILABLE = "SPLIT_CONSISTENCY_CHECK_UNAVAILABLE"
    PROVIDER_ONLY_COVERAGE_ACCEPTED = "PROVIDER_ONLY_COVERAGE_ACCEPTED"
    """An explicit policy accepted the provider's own claim; results must be labelled."""


FATAL_QUALITY: Final = frozenset({
    QualityIssue.BARS_MISSING, QualityIssue.BAR_ROW_INVALID, QualityIssue.BAR_FEED_MISMATCH,
    QualityIssue.IDENTITY_MISSING, QualityIssue.IDENTITY_MISMATCH, QualityIssue.UNKNOWN_BATCH,
    QualityIssue.CORPORATE_ACTIONS_MISSING, QualityIssue.CORPORATE_ACTIONS_INVALID,
    QualityIssue.COVERAGE_EVIDENCE_INSUFFICIENT, QualityIssue.OTHER_EVENT_COVERAGE_MISSING,
    QualityIssue.OTHER_EVENT_OUTSIDE_COVERAGE, QualityIssue.UNSUPPORTED_CORPORATE_ACTION,
    QualityIssue.EVIDENCE_UNVERIFIABLE,
    QualityIssue.SPLIT_DERIVATION_MISMATCH, QualityIssue.SERIES_REJECTED,
})  # fmt: skip


@dataclass(frozen=True, slots=True, order=True)
class QualityFinding:
    issue: QualityIssue
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SymbolQuality:
    symbol: str
    status: QualityStatus
    bar_count: int
    first_session: date | None
    last_session: date | None
    expected_sessions: int
    missing_sessions: tuple[date, ...]
    zero_volume_sessions: int
    findings: tuple[QualityFinding, ...]
    series_findings: tuple[str, ...]
    """``prepare_series`` findings (``ISSUE detail``) over the full dataset."""
    split_consistency: str = "UNAVAILABLE"
    """Same-provider raw vs split-adjusted check: PASSED, FAILED or UNAVAILABLE.
    Internal consistency only — never independent verification."""


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    dataset_id: str
    digest: str
    created_at: datetime
    feed: BarFeed
    research_start: date
    research_end: date
    warmup_start: date
    universe_id: str
    symbols: tuple[str, ...]
    survivorship_bias_free: bool
    batches: tuple[str, ...]
    raw: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ResearchDataset:
    manifest: DatasetManifest
    sessions: tuple[TradingSession, ...]
    identities: Mapping[str, SymbolIdentity]
    bars: Mapping[str, tuple[DailyBar, ...]]
    corporate_actions: Mapping[str, CorporateActions | None]
    """None for every rejected symbol (fails closed downstream)."""
    records: Mapping[str, CorporateActionRecord | None]
    quality: tuple[SymbolQuality, ...]

    @property
    def digest(self) -> str:
        return self.manifest.digest

    def accepted_symbols(self) -> tuple[str, ...]:
        return tuple(q.symbol for q in self.quality if q.status is not QualityStatus.REJECTED)

    def readiness(self) -> DatasetReadiness:
        return assess_readiness(self)

    def research_inputs(self, sectors: Mapping[str, str] | None = None) -> ResearchInputs:
        """The only bridge from a dataset to evaluation: refused
        (:class:`DatasetNotResearchReadyError`) unless the dataset is RESEARCH_READY.
        The inputs carry a :class:`ResearchAdmission` naming that readiness; the
        walk-forward entry points refuse inputs without one. Inspection
        (:func:`quality_report`, :meth:`readiness`) never needs this."""
        readiness = self.readiness()
        if not readiness.research_ready:
            raise DatasetNotResearchReadyError(readiness)
        files = _obj(self.manifest.raw.get("files"), "files")
        universe = self.manifest.raw.get("universe")
        return ResearchInputs(
            dataset_digest=self.digest, sessions=self.sessions, bars=dict(self.bars),
            corporate_actions=dict(self.corporate_actions), sectors=dict(sectors or {}),
            feed=self.manifest.feed, dividend_policy=DividendPolicy.REQUIRE_COVERAGE,
            file_checksums={rel: str(_obj(e, rel).get("sha256")) for rel, e in files.items()},
            universe_id=self.manifest.universe_id,
            universe_fingerprint=sha256(canonical(universe).encode()),
            admission=ResearchAdmission(self.digest, readiness.state.value,
                                        readiness.fingerprint),
        )  # fmt: skip


def _read_verified(root: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    if not root.is_dir():
        raise DatasetIntegrityError(f"{root} is not a dataset directory")
    refuse_ledger_location(root)
    if not (root / MANIFEST).is_file():
        raise DatasetIntegrityError(f"{root} has no {MANIFEST}")
    manifest = _obj(loads((root / MANIFEST).read_bytes()), MANIFEST)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise DatasetIntegrityError(
            f"unsupported schema_version {manifest.get('schema_version')!r}"
        )
    if manifest.get("manifest_sha256") != _manifest_digest(manifest):
        raise DatasetIntegrityError("manifest digest mismatch (the manifest was changed)")
    listed = _obj(manifest.get("files"), "files")
    present = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    present.discard(MANIFEST)
    if present != set(listed):
        extra, missing = sorted(present - set(listed)), sorted(set(listed) - present)
        raise DatasetIntegrityError(f"file set mismatch: unlisted {extra}, missing {missing}")
    blobs: dict[str, bytes] = {}
    for rel in sorted(listed):
        entry = _obj(listed[rel], rel)
        blob = (root / rel).read_bytes()
        if len(blob) != entry.get("bytes") or sha256(blob) != entry.get("sha256"):
            raise DatasetIntegrityError(f"{rel}: checksum mismatch")
        blobs[rel] = blob
    if manifest.get("corporate_action_claims") != corporate_action_claims(blobs):
        raise DatasetIntegrityError("manifest corporate-action claims differ from the files")
    return manifest, blobs


def _parse_manifest(m: Mapping[str, Any]) -> DatasetManifest:
    if m.get("market_timezone") != MARKET_TIMEZONE:
        raise DatasetFormatError(f"market_timezone must be {MARKET_TIMEZONE}")
    feed = m.get("feed")
    if feed not in ("sip", "iex"):
        raise DatasetFormatError("feed must be 'sip' or 'iex'")
    window = _obj(m.get("research_window"), "research_window")
    universe = _obj(m.get("universe"), "universe")
    symbols = tuple(_str(s, "symbol") for s in _list(universe.get("symbols"), "symbols"))
    if not symbols or list(symbols) != sorted(set(symbols)):
        raise DatasetFormatError("universe symbols must be non-empty, unique and sorted")
    for s in symbols:
        if not _SYMBOL_RE.match(s):
            raise DatasetFormatError(f"invalid symbol {s!r}")
    batches = []
    for item in _list(m.get("acquisitions"), "acquisitions"):
        b = _obj(item, "acquisition")
        for key in ("batch_id", "kind", "provider", "endpoint"):
            _str(b.get(key), f"acquisition {key}")
        _obj(b.get("params"), "acquisition params")
        _ts(b.get("requested_at"), "requested_at")
        _ts(b.get("received_at"), "received_at")
        _int(b.get("request_count"), "request_count")
        batches.append(str(b["batch_id"]))
    if len(set(batches)) != len(batches):
        raise DatasetFormatError("duplicate batch_id")
    survivorship = universe.get("survivorship_bias_free")
    if not isinstance(survivorship, bool):
        raise DatasetFormatError("universe.survivorship_bias_free must be a boolean")
    start = _day(window.get("start"), "research_window.start")
    end = _day(window.get("end"), "research_window.end")
    warmup = _day(m.get("warmup_start"), "warmup_start")
    if not warmup <= start <= end:
        raise DatasetFormatError("need warmup_start <= research start <= research end")
    return DatasetManifest(
        dataset_id=_str(m.get("dataset_id"), "dataset_id"), digest=str(m["manifest_sha256"]),
        created_at=_ts(m.get("created_at"), "created_at"), feed=feed, research_start=start,
        research_end=end, warmup_start=warmup,
        universe_id=_str(universe.get("id"), "universe.id"), symbols=symbols,
        survivorship_bias_free=survivorship, batches=tuple(batches), raw=dict(m),
    )  # fmt: skip


def _hhmm(value: object, name: str) -> time:
    text = _str(value, name)
    try:
        return time.fromisoformat(text)
    except ValueError:
        raise DatasetFormatError(f"{name}: invalid time {text!r}") from None


def _parse_calendar(blob: bytes, manifest: DatasetManifest) -> tuple[TradingSession, ...]:
    cal = _obj(loads(blob), CALENDAR)
    if cal.get("timezone") != MARKET_TIMEZONE:
        raise DatasetFormatError(f"calendar timezone must be {MARKET_TIMEZONE}")
    if cal.get("batch_id") not in manifest.batches:
        raise DatasetFormatError("calendar batch_id is not in the manifest")
    start, end = _day(cal.get("start"), "calendar.start"), _day(cal.get("end"), "calendar.end")
    if start > manifest.warmup_start or end < manifest.research_end:
        raise DatasetFormatError(
            f"calendar {start}..{end} does not cover "
            f"{manifest.warmup_start}..{manifest.research_end}"
        )
    sessions: list[TradingSession] = []
    for item in _list(cal.get("sessions"), "sessions"):
        row = _obj(item, "session")
        d = _day(row.get("date"), "session date")
        settle = _day(row.get("settlement_date"), f"{d} settlement_date")
        s = TradingSession(
            session_date=d,
            open_at=datetime.combine(d, _hhmm(row.get("open"), "open"), tzinfo=MARKET_TZ),
            close_at=datetime.combine(d, _hhmm(row.get("close"), "close"), tzinfo=MARKET_TZ),
            settlement_date=settle,
        )  # fmt: skip
        problems = s.problems()
        if problems or not start <= d <= end:
            raise DatasetFormatError(f"calendar session {d}: {problems or 'outside range'}")
        if sessions and d <= sessions[-1].session_date:
            raise DatasetFormatError(f"calendar not strictly increasing at {d}")
        sessions.append(s)
    if not sessions:
        raise DatasetFormatError("calendar has no sessions")
    return tuple(sessions)


def _parse_identity(blob: bytes) -> dict[str, SymbolIdentity]:
    doc = _obj(loads(blob), IDENTITY)
    out: dict[str, SymbolIdentity] = {}
    for sym, item in sorted(_obj(doc.get("symbols"), "symbols").items()):
        raw = _obj(item, sym)
        spans = tuple(
            TickerSpan(_str(t.get("ticker"), "ticker"), _day(t.get("start"), "ticker start"),
                       _opt_day(t.get("end"), "ticker end"))
            for t in (_obj(x, "ticker") for x in _list(raw.get("tickers"), "tickers"))
        )  # fmt: skip
        if not spans:
            raise DatasetFormatError(f"{sym}: no ticker history")
        for a, b in itertools.pairwise(spans):
            if a.end is None or b.start <= a.end:
                raise DatasetFormatError(f"{sym}: ticker spans overlap or are unordered")
        out[sym] = SymbolIdentity(sym, spans, _opt_day(raw.get("listing_start"), "listing_start"),
                                  _opt_day(raw.get("delisted_on"), "delisted_on"))  # fmt: skip
    return out


def _parse_bar(row: Mapping[str, Any], symbol: str, manifest: DatasetManifest,
               adjustment: BarAdjustment) -> tuple[DailyBar, str, str]:  # fmt: skip
    if row.get("symbol") != symbol:
        raise DatasetFormatError(f"row symbol {row.get('symbol')!r} in {symbol}'s file")
    if row.get("feed") != manifest.feed or row.get("adjustment") != adjustment:
        raise DatasetFormatError("feed/adjustment differ from the manifest/file")
    vw, n = row.get("vw"), row.get("n")
    try:
        bar = DailyBar(
            symbol=symbol, session_date=_day(row.get("session"), "session"),
            open=_dec(row.get("o"), "o"), high=_dec(row.get("h"), "h"),
            low=_dec(row.get("l"), "l"), close=_dec(row.get("c"), "c"),
            volume=_int(row.get("v"), "v"), vwap=None if vw is None else _dec(vw, "vw"),
            trade_count=None if n is None else _int(n, "n"), feed=manifest.feed,
            adjustment=adjustment, as_of=_ts(row.get("t"), "t"),
            received_at=_ts(row.get("received_at"), "received_at"),
        )  # fmt: skip
    except ValueError as exc:
        raise DatasetFormatError(str(exc)) from None
    return bar, _str(row.get("source_symbol"), "source_symbol"), _str(row.get("batch_id"),
                                                                       "batch_id")  # fmt: skip


def _parse_bars(
    blob: bytes,
    symbol: str,
    manifest: DatasetManifest,
    adjustment: BarAdjustment,
    identity: SymbolIdentity | None,
) -> tuple[tuple[DailyBar, ...], list[QualityFinding]]:
    bars: list[DailyBar] = []
    findings: list[QualityFinding] = []
    for i, line in enumerate(blob.decode().splitlines()):
        try:
            bar, source, batch = _parse_bar(_obj(loads(line), "bar"), symbol, manifest, adjustment)
        except DatasetFormatError as exc:
            return (), [QualityFinding(QualityIssue.BAR_ROW_INVALID, f"line {i + 1}: {exc}")]
        if batch not in manifest.batches:
            findings.append(QualityFinding(QualityIssue.UNKNOWN_BATCH, f"{batch} line {i + 1}"))
        expected = identity.ticker_on(bar.session_date) if identity else None
        if identity is not None and source != expected:
            findings.append(
                QualityFinding(
                    QualityIssue.IDENTITY_MISMATCH, f"{bar.session_date}: {source} != {expected}"
                )
            )
        bars.append(bar)
    return tuple(bars), sorted(set(findings))[:20]


def _coverage(raw: object, name: str) -> Coverage | None:
    if raw is None:
        return None
    c = _obj(raw, name)
    evidence = []
    for item in _list(c.get("evidence"), f"{name}.evidence"):
        e = _obj(item, f"{name} evidence")
        try:
            kind = EvidenceKind(str(e.get("kind")))
        except ValueError:
            raise DatasetFormatError(f"{name}: unknown evidence {e.get('kind')!r}") from None
        evidence.append(Evidence(kind, _str(e.get("source"), "evidence source"),
                                 _str(e.get("reference"), "evidence reference")))  # fmt: skip
    start, end = _day(c.get("start"), f"{name}.start"), _day(c.get("end"), f"{name}.end")
    return Coverage(start, end, tuple(evidence))


def _parse_corporate_actions(blob: bytes, symbol: str) -> CorporateActionRecord:
    doc = _obj(loads(blob), "corporate actions")
    if doc.get("symbol") != symbol:
        raise DatasetFormatError(f"record for {doc.get('symbol')!r} in {symbol}'s file")
    try:
        splits, lieu = [], []
        for s in (_obj(x, "split") for x in _list(doc.get("splits"), "splits")):
            ev = SplitEvent(symbol, _day(s.get("effective_session"), "effective_session"),
                            _int(s.get("new_shares"), "new_shares"),
                            _int(s.get("old_shares"), "old_shares"),
                            _str(s.get("source"), "split source"))  # fmt: skip
            splits.append(ev)
            if s.get("cash_in_lieu_price") is not None:
                lieu.append((ev.effective_session, _dec(s["cash_in_lieu_price"], "cash in lieu")))
        dividends, kinds = [], []
        for d in (_obj(x, "dividend") for x in _list(doc.get("dividends"), "dividends")):
            div = CashDividend(symbol, _day(d.get("ex_session"), "ex_session"),
                               _day(d.get("pay_date"), "pay_date"), _dec(d.get("amount"), "amount"),
                               _str(d.get("source"), "dividend source"))  # fmt: skip
            dividends.append(div)
            kinds.append((div.ex_session, DistributionKind(str(d.get("kind")))))
        others = [
            OtherEvent(
                OtherEventKind(str(e.get("kind"))),
                _day(e.get("effective_session"), "effective_session"),
                str(e.get("detail", "")),
                _str(e.get("source"), "event source"),
            )
            for e in (_obj(x, "event") for x in _list(doc.get("other_events"), "events"))
        ]
    except (DataContractError, ValueError) as exc:
        if isinstance(exc, DatasetFormatError):
            raise
        raise DatasetFormatError(str(exc)) from None
    return CorporateActionRecord(
        symbol, tuple(splits), tuple(lieu), tuple(dividends), tuple(kinds), tuple(others),
        _coverage(doc.get("split_coverage"), "split_coverage"),
        _coverage(doc.get("dividend_coverage"), "dividend_coverage"),
        _coverage(doc.get("other_coverage"), "other_coverage"),
    )  # fmt: skip


def _judge(
    name: str,
    cov: Coverage | None,
    accepted: frozenset[EvidenceKind],
    out: list[QualityFinding],
) -> Coverage | None:
    """``cov`` when its evidence establishes completeness under ``accepted``, else None
    (with a fatal finding). Same-provider consistency never counts on its own."""
    if cov is None:
        return None
    usable = (cov.kinds - NEVER_SUFFICIENT) & accepted
    kinds = "+".join(sorted(k.value for k in cov.kinds))
    if not usable:
        out.append(QualityFinding(QualityIssue.COVERAGE_EVIDENCE_INSUFFICIENT, f"{name}: {kinds}"))
        return None
    if usable == {EvidenceKind.PROVIDER_REPORTED}:
        out.append(QualityFinding(QualityIssue.PROVIDER_ONLY_COVERAGE_ACCEPTED, name))
    return cov


def _accept(
    rec: CorporateActionRecord,
    bars: Sequence[DailyBar],
    identity: SymbolIdentity | None,
    policy: CoverageAcceptance,
) -> tuple[CorporateActions | None, list[QualityFinding]]:
    """Apply the acceptance policy; returns the record usable by ``prepare_series``."""
    out: list[QualityFinding] = []
    split_cov = _judge("splits", rec.split_coverage, policy.splits, out)
    div_cov = _judge("dividends", rec.dividend_coverage, policy.dividends, out)
    other = _judge("other events", rec.other_coverage, policy.other_events, out)
    if rec.other_coverage is None:
        out.append(QualityFinding(QualityIssue.OTHER_EVENT_COVERAGE_MISSING, "no attestation"))
    elif other is None:
        pass  # insufficient evidence already reported (fatal)
    elif bars and not (other.start <= bars[0].session_date and bars[-1].session_date <= other.end):
        out.append(QualityFinding(QualityIssue.OTHER_EVENT_COVERAGE_MISSING,
                                  f"covered {other.start}..{other.end}"))  # fmt: skip
    out += _event_findings(rec, bars, identity)
    if any(f.issue in FATAL_QUALITY for f in out):
        return None, out
    ca = CorporateActions(rec.symbol, rec.splits, rec.dividends,
                          split_cov.window() if split_cov else None,
                          div_cov.window() if div_cov else None)  # fmt: skip
    return ca, out


def _event_findings(rec: CorporateActionRecord, bars: Sequence[DailyBar],
                    identity: SymbolIdentity | None) -> list[QualityFinding]:  # fmt: skip
    out: list[QualityFinding] = []
    span = (bars[0].session_date, bars[-1].session_date) if bars else None
    for e in rec.other_events:
        if rec.other_coverage is not None and not rec.other_coverage.contains(e.effective_session):
            out.append(QualityFinding(QualityIssue.OTHER_EVENT_OUTSIDE_COVERAGE,
                                      f"{e.kind.value} {e.effective_session}"))  # fmt: skip
        inside = span is not None and span[0] < e.effective_session <= span[1]
        if e.kind in UNSUPPORTED_EVENTS and inside:
            out.append(
                QualityFinding(
                    QualityIssue.UNSUPPORTED_CORPORATE_ACTION,
                    f"{e.kind.value} {e.effective_session}: {e.detail}",
                )
            )
        if e.kind is OtherEventKind.NAME_CHANGE and identity is not None:
            starts = {t.start for t in identity.tickers[1:]}
            if e.effective_session not in starts:
                out.append(
                    QualityFinding(
                        QualityIssue.IDENTITY_MISMATCH,
                        f"name change {e.effective_session} not in identity",
                    )
                )
    return out


def _split_consistency(rec: CorporateActionRecord, raw: Sequence[DailyBar],
                       adjusted: Sequence[DailyBar] | None) -> list[QualityFinding]:  # fmt: skip
    if adjusted is None:
        return [QualityFinding(QualityIssue.SPLIT_CONSISTENCY_CHECK_UNAVAILABLE,
                               "no adjusted bars")]  # fmt: skip
    derived = derive_split_events(rec.symbol, raw, adjusted)
    if not derived.resolved:
        issues = ",".join(sorted({f.issue.value for f in derived.findings}))
        return [QualityFinding(QualityIssue.SPLIT_DERIVATION_MISMATCH, f"unresolved: {issues}")]
    span = (raw[0].session_date, raw[-1].session_date) if raw else None
    recorded = {(s.effective_session, s.ratio) for s in rec.splits
                if span is not None and span[0] < s.effective_session <= span[1]}  # fmt: skip
    found = {(s.effective_session, s.ratio) for s in derived.events}
    if recorded != found:
        detail = (f"recorded {sorted(str(d) for d, _ in recorded)} "
                  f"derived {sorted(str(d) for d, _ in found)}")  # fmt: skip
        return [QualityFinding(QualityIssue.SPLIT_DERIVATION_MISMATCH, detail)]
    return []


def _coverage_quality(symbol: str, bars: Sequence[DailyBar], sessions: Sequence[TradingSession],
                      manifest: DatasetManifest, identity: SymbolIdentity | None,
                      ) -> tuple[int, tuple[date, ...], list[QualityFinding]]:  # fmt: skip
    lo = manifest.warmup_start
    if identity is not None and identity.listing_start is not None:
        lo = max(lo, identity.listing_start)
    hi = manifest.research_end
    if identity is not None and identity.delisted_on is not None:
        hi = min(hi, identity.delisted_on)
    expected = [s.session_date for s in sessions if lo <= s.session_date <= hi]
    have = {b.session_date for b in bars}
    missing = tuple(d for d in expected if d not in have)
    out: list[QualityFinding] = []
    if bars and expected and bars[0].session_date > expected[0]:
        out.append(QualityFinding(QualityIssue.HISTORY_STARTS_LATE,
                                  f"{symbol} first bar {bars[0].session_date}"))  # fmt: skip
    if bars and expected and bars[-1].session_date < expected[-1]:
        out.append(QualityFinding(QualityIssue.HISTORY_ENDS_EARLY,
                                  f"{symbol} last bar {bars[-1].session_date}"))  # fmt: skip
    if missing:
        shown = ", ".join(str(d) for d in missing[:5]) + (" ..." if len(missing) > 5 else "")
        out.append(QualityFinding(QualityIssue.MISSING_SESSIONS, f"{len(missing)}: {shown}"))
    zero = sum(1 for b in bars if b.volume == 0)
    if zero:
        out.append(QualityFinding(QualityIssue.ZERO_VOLUME_SESSIONS, str(zero)))
    return len(expected), missing, out


def _unverifiable(rec: CorporateActionRecord, *, adjusted_present: bool) -> list[QualityFinding]:
    """Same-provider consistency evidence must be re-checkable from the dataset itself:
    for splits, the provider's split-adjusted bars must be stored; for other lists no
    such check exists, so the claim is unverifiable."""
    out: list[QualityFinding] = []
    same = EvidenceKind.SAME_PROVIDER_CONSISTENCY
    for name, cov, checkable in (("splits", rec.split_coverage, adjusted_present),
                                 ("dividends", rec.dividend_coverage, False),
                                 ("other events", rec.other_coverage, False)):  # fmt: skip
        if cov is not None and same in cov.kinds and not checkable:
            out.append(QualityFinding(QualityIssue.EVIDENCE_UNVERIFIABLE, f"{name}: {same.value}"))
    return out


@dataclass(slots=True)
class _SymbolLoad:
    bars: tuple[DailyBar, ...] = ()
    ca: CorporateActions | None = None
    record: CorporateActionRecord | None = None
    findings: list[QualityFinding] = field(default_factory=list)
    split_consistency: str = "UNAVAILABLE"


def _load_symbol(
    sym: str,
    blobs: Mapping[str, bytes],
    manifest: DatasetManifest,
    identity: SymbolIdentity | None,
    policy: CoverageAcceptance,
) -> _SymbolLoad:
    out = _SymbolLoad()
    if identity is None:
        out.findings.append(QualityFinding(QualityIssue.IDENTITY_MISSING, sym))
    raw_blob = blobs.get(f"bars/{sym}.raw.jsonl")
    if raw_blob is None:
        out.findings.append(QualityFinding(QualityIssue.BARS_MISSING, "no raw bar file"))
        return out
    out.bars, found = _parse_bars(raw_blob, sym, manifest, "raw", identity)
    out.findings += found
    adj_blob = blobs.get(f"bars/{sym}.split.jsonl")
    adjusted: tuple[DailyBar, ...] | None = None
    if adj_blob is not None:
        adjusted, found = _parse_bars(adj_blob, sym, manifest, "split", identity)
        out.findings += found
    ca_blob = blobs.get(f"corporate_actions/{sym}.json")
    if ca_blob is None:
        out.findings.append(QualityFinding(QualityIssue.CORPORATE_ACTIONS_MISSING, "no record"))
        return out
    try:
        out.record = _parse_corporate_actions(ca_blob, sym)
    except DatasetFormatError as exc:
        out.findings.append(QualityFinding(QualityIssue.CORPORATE_ACTIONS_INVALID, str(exc)))
        return out
    out.ca, found = _accept(out.record, out.bars, identity, policy)
    out.findings += found
    consistency = _split_consistency(out.record, out.bars, adjusted)
    out.findings += consistency
    out.split_consistency = (
        "UNAVAILABLE" if adjusted is None else "FAILED" if consistency else "PASSED"
    )
    out.findings += _unverifiable(out.record, adjusted_present=adjusted is not None)
    if any(f.issue in FATAL_QUALITY for f in out.findings):
        out.ca = None
    return out


def load_dataset(root: Path, *, acceptance: CoverageAcceptance | None = None) -> ResearchDataset:
    """Verify and load a dataset. Integrity and schema faults raise; per-symbol data
    faults reject that symbol (reported in ``quality``) and never alter its data."""
    policy = acceptance or CoverageAcceptance()
    raw_manifest, blobs = _read_verified(root)
    manifest = _parse_manifest(raw_manifest)
    sessions = _parse_calendar(blobs[CALENDAR], manifest) if CALENDAR in blobs else None
    if sessions is None or IDENTITY not in blobs:
        raise DatasetFormatError("calendar.json and identity.json are required")
    identities = _parse_identity(blobs[IDENTITY])
    calendar = SessionCalendar(sessions)
    bars: dict[str, tuple[DailyBar, ...]] = {}
    actions: dict[str, CorporateActions | None] = {}
    records: dict[str, CorporateActionRecord | None] = {}
    quality: list[SymbolQuality] = []
    for sym in manifest.symbols:
        identity = identities.get(sym)
        loaded = _load_symbol(sym, blobs, manifest, identity, policy)
        expected, missing, coverage = _coverage_quality(sym, loaded.bars, sessions, manifest,
                                                        identity)  # fmt: skip
        findings = loaded.findings + coverage
        prep = prepare_series(sym, loaded.bars, calendar=calendar,
                              corporate_actions=loaded.ca, feed=manifest.feed)  # fmt: skip
        series_findings = tuple(f"{f.issue.value} {f.detail}".strip() for f in prep.findings)
        if loaded.ca is not None and not prep.accepted:
            fatal = sorted({f.issue.value for f in prep.findings if f.issue in FATAL_ISSUES})
            findings.append(QualityFinding(QualityIssue.SERIES_REJECTED, ",".join(fatal)))
        if any(f.issue.value == "PRICE_DISCONTINUITY_UNEXPLAINED" for f in prep.findings):
            findings.append(QualityFinding(QualityIssue.PRICE_DISCONTINUITY_UNEXPLAINED))
        ordered = tuple(sorted(set(findings)))
        fatal_found = any(f.issue in FATAL_QUALITY for f in ordered) or loaded.ca is None
        status = (
            QualityStatus.REJECTED
            if fatal_found
            else QualityStatus.ACCEPTED_WITH_WARNINGS
            if ordered
            else QualityStatus.ACCEPTED
        )
        bars[sym] = loaded.bars
        actions[sym] = None if fatal_found else loaded.ca
        records[sym] = loaded.record
        quality.append(SymbolQuality(
            symbol=sym, status=status, bar_count=len(loaded.bars),
            first_session=loaded.bars[0].session_date if loaded.bars else None,
            last_session=loaded.bars[-1].session_date if loaded.bars else None,
            expected_sessions=expected, missing_sessions=missing,
            zero_volume_sessions=sum(1 for b in loaded.bars if b.volume == 0),
            findings=ordered, series_findings=series_findings,
            split_consistency=loaded.split_consistency,
        ))  # fmt: skip
    return ResearchDataset(manifest, sessions, identities, bars, actions, records, tuple(quality))


# ===================================================================== readiness

#: ``acquisition_report.acquisition_state`` written by the staging assembler.
ACQUISITION_COMPLETE: Final = "ACQUISITION_COMPLETE"
_HEX64: Final = re.compile(r"^[0-9a-f]{64}$")
_COVERAGE_LISTS: Final = (("splits", "split_coverage"), ("dividends", "dividend_coverage"),
                          ("other_events", "other_coverage"))  # fmt: skip


class DatasetState(StrEnum):
    """Two separate gates, in order. A dataset can be complete and still not usable."""

    ACQUISITION_INCOMPLETE = "ACQUISITION_INCOMPLETE"
    ACQUISITION_COMPLETE = "ACQUISITION_COMPLETE"
    """Every required provider batch is present, checksummed and validated."""
    RESEARCH_READY = RESEARCH_READY
    """ACQUISITION_COMPLETE, every universe symbol passed the quality checks, and every
    corporate-action list of every symbol carries an accepted attestation
    (INDEPENDENT_SOURCE or OPERATOR_REVIEW) over the whole dataset window."""


@dataclass(frozen=True, slots=True)
class DatasetReadiness:
    dataset_digest: str
    acquisition_blockers: tuple[str, ...]
    research_blockers: tuple[str, ...]
    """Reasons the dataset is not research-ready (empty when it is). Includes
    ``ACQUISITION_INCOMPLETE`` when the first gate failed."""

    @property
    def acquisition_complete(self) -> bool:
        return not self.acquisition_blockers

    @property
    def research_ready(self) -> bool:
        return self.acquisition_complete and not self.research_blockers

    @property
    def state(self) -> DatasetState:
        if not self.acquisition_complete:
            return DatasetState.ACQUISITION_INCOMPLETE
        return DatasetState.RESEARCH_READY if self.research_ready \
            else DatasetState.ACQUISITION_COMPLETE  # fmt: skip

    def to_dict(self) -> dict[str, object]:
        return {"dataset_digest": self.dataset_digest, "state": self.state.value,
                "acquisition_complete": self.acquisition_complete,
                "research_ready": self.research_ready,
                "acquisition_blockers": list(self.acquisition_blockers),
                "research_blockers": list(self.research_blockers)}  # fmt: skip

    @property
    def fingerprint(self) -> str:
        return sha256(canonical(self.to_dict()).encode())


def _acquisition_blockers(m: DatasetManifest) -> list[str]:
    report = m.raw.get("acquisition_report")
    if not isinstance(report, dict):
        return ["ACQUISITION_REPORT_MISSING"]
    out = []
    if report.get("acquisition_state") != ACQUISITION_COMPLETE:
        out.append(f"ACQUISITION_STATE {report.get('acquisition_state')!r}")
    required = report.get("required_batches")
    if not isinstance(required, dict) or not required:
        return [*out, "REQUIRED_BATCHES_MISSING"]
    present = set(m.batches)
    for bid, entry in sorted(required.items()):
        if bid not in present:
            out.append(f"REQUIRED_BATCH_ABSENT {bid}")
        pages = entry.get("pages_sha256") if isinstance(entry, dict) else None
        if (not isinstance(pages, list) or not pages
                or not all(isinstance(h, str) and _HEX64.match(h) for h in pages)):  # fmt: skip
            out.append(f"REQUIRED_BATCH_UNCHECKSUMMED {bid}")
    out += [f"UNREQUIRED_BATCH {bid}" for bid in sorted(present - set(required))]
    return out


def _research_blockers(ds: ResearchDataset) -> list[str]:
    m = ds.manifest
    out = []
    for q in ds.quality:
        if q.status is QualityStatus.REJECTED:
            issues = ",".join(sorted({f.issue.value for f in q.findings})) or "NO_RECORD"
            out.append(f"SYMBOL_REJECTED {q.symbol}: {issues}")
        if any(f.issue is QualityIssue.PROVIDER_ONLY_COVERAGE_ACCEPTED for f in q.findings):
            out.append(f"PROVIDER_ONLY_COVERAGE {q.symbol}")
    for sym in m.symbols:
        rec = ds.records.get(sym)
        for name, attr in _COVERAGE_LISTS:
            cov = None if rec is None else getattr(rec, attr)
            if (cov is None or not cov.kinds & _ATTESTING
                    or cov.start > m.warmup_start or cov.end < m.research_end):  # fmt: skip
                out.append(f"ATTESTATION_MISSING {sym} {name}")
    return out


def assess_readiness(ds: ResearchDataset) -> DatasetReadiness:
    """Both gates for a loaded (integrity-verified) dataset. Pure; never raises for a
    data fault: every reason is listed so a non-ready dataset stays inspectable."""
    acquisition = _acquisition_blockers(ds.manifest)
    research = _research_blockers(ds)
    if acquisition:
        research.insert(0, "ACQUISITION_INCOMPLETE")
    return DatasetReadiness(ds.digest, tuple(acquisition), tuple(research))


def quality_report(ds: ResearchDataset) -> dict[str, Any]:
    """Every universe symbol with its status and reasons, rejected ones included, plus the
    acquisition findings recorded in the manifest (Step 16.16E). Nothing is dropped."""
    acquisition = ds.manifest.raw.get("acquisition_report")
    acq = acquisition if isinstance(acquisition, dict) else {}
    rows = [{
        "symbol": q.symbol, "status": q.status.value, "bar_count": q.bar_count,
        "first_session": None if q.first_session is None else str(q.first_session),
        "last_session": None if q.last_session is None else str(q.last_session),
        "expected_sessions": q.expected_sessions, "missing_sessions": len(q.missing_sessions),
        "split_consistency": q.split_consistency,
        "findings": [f"{f.issue.value} {f.detail}".strip() for f in q.findings],
        "acquisition_findings": [f for f in acq.get("findings", [])
                                 if isinstance(f, dict) and f.get("symbol") in (q.symbol, "*")],
    } for q in ds.quality]  # fmt: skip
    return {
        "dataset_digest": ds.digest,
        "readiness": assess_readiness(ds).to_dict(),
        "survivorship_bias_free": ds.manifest.survivorship_bias_free,
        "accepted": [r["symbol"] for r in rows if r["status"] != QualityStatus.REJECTED.value],
        "rejected": {str(r["symbol"]): r["findings"] for r in rows
                     if r["status"] == QualityStatus.REJECTED.value},
        "symbols": rows,
        "ca_date_semantics": acq.get("ca_date_semantics"),
        "corporate_action_claims": acq.get("corporate_action_claims"),
    }  # fmt: skip


__all__ = [
    "ACQUISITION_COMPLETE",
    "CALENDAR",
    "IDENTITY",
    "MANIFEST",
    "SCHEMA_VERSION",
    "UNSUPPORTED_EVENTS",
    "CorporateActionRecord",
    "Coverage",
    "CoverageAcceptance",
    "DatasetError",
    "DatasetFormatError",
    "DatasetIntegrityError",
    "DatasetManifest",
    "DatasetNotResearchReadyError",
    "DatasetReadiness",
    "DatasetState",
    "DistributionKind",
    "Evidence",
    "EvidenceKind",
    "OtherEvent",
    "OtherEventKind",
    "QualityFinding",
    "QualityIssue",
    "QualityStatus",
    "ResearchDataset",
    "SymbolIdentity",
    "SymbolQuality",
    "TickerSpan",
    "assess_readiness",
    "bars_from_alpaca_pages",
    "canonical",
    "corporate_action_claims",
    "encode_bar",
    "encode_calendar",
    "encode_corporate_actions",
    "encode_identity",
    "encode_json",
    "encode_jsonl",
    "load_dataset",
    "loads",
    "quality_report",
    "refuse_ledger_location",
    "sha256",
    "write_dataset",
]
