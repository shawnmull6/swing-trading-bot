"""Corporate-action responses: strict parsing, explicit date semantics and padded
windows (Step 16.16E; RESEARCH_DATA §3.3a).

**Six distinct dates — never conflated.**

* the *API request window* — the ``start``/``end`` sent (``CorporateActionWindow``
  ``request_start``/``request_end``, padded);
* the *provider filter date* — whichever event date the provider compares with that
  window. The 16.16D probe proved it is **not** the ex-date or the record date and is
  consistent with the payable or process date; it is otherwise unverified
  (:class:`FilterSemantics`, gate ``CA-DATE-SEMANTICS``);
* the *ex-date* — the first session the security trades without the entitlement;
* the *effective date* — the session an event changes the series: the ex-date for
  splits and dividends; for other types the ex-date or, without one, the provider's
  ``effective_date`` field (no other field is guessed);
* the *payable date* — when a cash distribution is paid (``CashDividend.pay_date``);
* the *research coverage window* — the dates whose events a batch supplies, selected
  by **effective date** (``coverage_start``/``coverage_end``), never by the filter date.

**Policy.** Each batch's request is padded on both sides; every returned event is
classified by its effective date: inside the batch's coverage slice it is kept,
otherwise it is a pad-zone observation used only for cross-checks. Coverage slices
are contiguous and disjoint; the padded requests overlap, so the same event is seen
by neighbouring batches and is de-duplicated by provider id (a conflicting duplicate
fails closed). Padding is a mitigation, never proof: a ``PROVIDER_REPORTED`` coverage
claim is emitted only when the filter semantics are live-verified, every observed lag
sits inside the padding with a margin, every pad-zone observation is confirmed by the
batch that owns it, and the slices are contiguous. Even then the claim is the
provider's own and is not accepted by the default ``CoverageAcceptance`` policy.
Independent or operator-review evidence can be attached explicitly.

This module performs no I/O and no request.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from swing_trading_bot.research.acquisition import (
    CA_PAD_MARGIN_DAYS,
    CA_SEMANTICS_REFERENCES,
    CorporateActionWindow,
    SemanticsReference,
)
from swing_trading_bot.research.dataset import (
    CorporateActionRecord,
    Coverage,
    DistributionKind,
    Evidence,
    EvidenceKind,
    OtherEvent,
    OtherEventKind,
    sha256,
)
from swing_trading_bot.strategy.market_data import CashDividend, DataContractError, SplitEvent

PROVIDER_SOURCE: Final = "alpaca:/v1/corporate-actions"
LISTS: Final = ("splits", "dividends", "other_events")


class CorporateActionParseError(ValueError):
    """A response the importer does not understand. ``code`` names the reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class DateRole(StrEnum):
    EX_DATE = "ex_date"
    RECORD_DATE = "record_date"
    PAYABLE_DATE = "payable_date"
    PROCESS_DATE = "process_date"


class SemanticsStatus(StrEnum):
    UNVERIFIED = "UNVERIFIED"
    CONSISTENT_WITH_OBSERVATIONS = "CONSISTENT_WITH_OBSERVATIONS"
    """Observations exclude some roles; the rest remain possible. Not verified."""
    LIVE_VERIFIED = "LIVE_VERIFIED"
    """The discriminating requests (ca-date-semantics-probe-v1) identified one role."""
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class FilterSemantics:
    status: SemanticsStatus
    candidates: tuple[DateRole, ...]
    """Roles not yet excluded (the filter is assumed to be one of them)."""
    evidence: str
    scope: tuple[str, ...] = ()
    """Response list names (action types) for which a LIVE_VERIFIED role was actually
    discriminated from every other candidate. Outside it the role is not verified."""
    fallback: tuple[DateRole, ...] = ()
    """For a LIVE_VERIFIED result: the roles still possible for action types outside
    ``scope`` (the prior candidates). Ignored otherwise."""

    @property
    def verified_role(self) -> DateRole | None:
        """The identified role, whatever its scope (use :meth:`role_for` per type)."""
        if self.status is SemanticsStatus.LIVE_VERIFIED and len(self.candidates) == 1:
            return self.candidates[0]
        return None

    def role_for(self, action_type: str) -> DateRole | None:
        """The verified filter role for one action type, or None (unverified there)."""
        return self.verified_role if action_type in self.scope else None

    def roles_for(self, action_type: str) -> tuple[DateRole, ...]:
        """Roles whose padding must be checked for an event of ``action_type``."""
        role = self.role_for(action_type)
        if role is not None:
            return (role,)
        if self.status is SemanticsStatus.LIVE_VERIFIED and self.fallback:
            return self.fallback
        return self.candidates


#: What the 16.16D probe established (history-probe-20261117T025839Z, P6).
OBSERVED_16_16D: Final = FilterSemantics(
    SemanticsStatus.CONSISTENT_WITH_OBSERVATIONS,
    (DateRole.PAYABLE_DATE, DateRole.PROCESS_DATE),
    "history-probe-20261117T025839Z P6: SPY ex 2020-06-19 (record 06-22, payable and "
    "process 07-31) returned for start 2020-07-01; ex-date and record date excluded",
)

ANY_LISTED_DATE: Final = "ANY_LISTED_DATE"
"""Hypothesis: an event is returned when ANY of its dates falls in the window."""


def _matches(ref: SemanticsReference, role: DateRole) -> bool:
    d = ref.dates()[role.value]
    return d is not None and d == ref.requested_day


def expected_pattern(hypothesis: DateRole | str) -> tuple[bool, ...]:
    """Whether each reference request (Q1, Q2, ... in order) returns its event under
    ``hypothesis`` — derived from the recorded event dates, never hand-written."""
    refs = [CA_SEMANTICS_REFERENCES[q] for q in sorted(CA_SEMANTICS_REFERENCES)]
    if hypothesis == ANY_LISTED_DATE:
        return tuple(any(_matches(r, role) for role in DateRole) for r in refs)
    return tuple(_matches(r, DateRole(hypothesis)) for r in refs)


def hypothesis_table() -> dict[str, tuple[bool, ...]]:
    return {h: expected_pattern(h) for h in (*(r.value for r in DateRole), ANY_LISTED_DATE)}


def _identifying_patterns() -> dict[tuple[bool, ...], DateRole]:
    """Patterns produced by exactly one hypothesis and containing at least one returned
    event (an all-empty answer is indistinguishable from a refused single-day window)."""
    table = hypothesis_table()
    out: dict[tuple[bool, ...], DateRole] = {}
    for role in DateRole:
        pattern = table[role.value]
        if any(pattern) and sum(1 for p in table.values() if p == pattern) == 1:
            out[pattern] = role
    return out


def discriminated_types(role: DateRole, others: Sequence[DateRole]) -> tuple[str, ...]:
    """Action types on which the references separate ``role`` from every role in
    ``others``: some reference of that type returns under one but not the other."""
    types = sorted({r.list_name for r in CA_SEMANTICS_REFERENCES.values()})
    return tuple(
        t for t in types
        if all(any(_matches(r, role) != _matches(r, o)
                   for r in CA_SEMANTICS_REFERENCES.values() if r.list_name == t)
               for o in others if o is not role)
    )  # fmt: skip


def evaluate_semantics_probe(
    returned: Mapping[str, bool], *, prior: FilterSemantics = OBSERVED_16_16D
) -> FilterSemantics:
    """Pure evaluation of ca-date-semantics-probe-v1: ``returned[Qn]`` says whether the
    response contained the reference event. Any other pattern, a missing answer or a role
    already excluded by ``prior`` is INCONCLUSIVE (fail closed). A verified role is scoped
    to the action types whose references actually discriminated it."""
    if set(returned) != set(CA_SEMANTICS_REFERENCES):
        return FilterSemantics(SemanticsStatus.INCONCLUSIVE, prior.candidates,
                               "not every reference request was answered")  # fmt: skip
    key = tuple(bool(returned[q]) for q in sorted(CA_SEMANTICS_REFERENCES))
    role = _identifying_patterns().get(key)
    if role is None or role not in prior.candidates:
        return FilterSemantics(SemanticsStatus.INCONCLUSIVE, prior.candidates,
                               f"pattern {key} identifies no admissible role")  # fmt: skip
    scope = discriminated_types(role, prior.candidates)
    return FilterSemantics(SemanticsStatus.LIVE_VERIFIED, (role,),
                           f"pattern {key}; discriminated on {list(scope)} only", scope,
                           prior.candidates)  # fmt: skip


def discriminating_events(
    events: Sequence[ProviderEvent],
    a: DateRole = DateRole.PAYABLE_DATE,
    b: DateRole = DateRole.PROCESS_DATE,
) -> tuple[ProviderEvent, ...]:
    """Events (e.g. from a separately authorized discovery listing) whose ``a`` and ``b``
    dates are both present and differ: the only events a single-day request can use to
    tell the two roles apart. Pure; performs no request."""
    out = []
    for ev in events:
        da, db = ev.date_of(a), ev.date_of(b)
        if da is not None and db is not None and da != db:
            out.append(ev)
    return tuple(out)


# ===================================================================== parsing


class EventCategory(StrEnum):
    SPLIT = "splits"
    DIVIDEND = "dividends"
    OTHER = "other_events"


@dataclass(frozen=True, slots=True)
class _TypeSpec:
    category: EventCategory
    other: OtherEventKind | None = None


_O: Final = EventCategory.OTHER
#: Response list name -> mapping. A list name not here fails the parse. Only
#: ``forward_splits`` and ``cash_dividends`` were observed (16.16D); the others are
#: mapped from the request type names and fail closed on any unrecognized shape.
ACTION_TYPES: Final = {
    "forward_splits": _TypeSpec(EventCategory.SPLIT),
    "reverse_splits": _TypeSpec(EventCategory.SPLIT),
    "cash_dividends": _TypeSpec(EventCategory.DIVIDEND),
    "unit_splits": _TypeSpec(_O, OtherEventKind.OTHER),
    "stock_dividends": _TypeSpec(_O, OtherEventKind.STOCK_DIVIDEND),
    "spin_offs": _TypeSpec(_O, OtherEventKind.SPIN_OFF),
    "cash_mergers": _TypeSpec(_O, OtherEventKind.MERGER),
    "stock_mergers": _TypeSpec(_O, OtherEventKind.MERGER),
    "stock_and_cash_mergers": _TypeSpec(_O, OtherEventKind.MERGER),
    "name_changes": _TypeSpec(_O, OtherEventKind.NAME_CHANGE),
    "worthless_removals": _TypeSpec(_O, OtherEventKind.DELISTING),
    "rights_distributions": _TypeSpec(_O, OtherEventKind.RIGHTS_DISTRIBUTION),
}
VERIFIED_SHAPES: Final = frozenset({"forward_splits", "cash_dividends"})
_TOP_KEYS: Final = frozenset({"corporate_actions", "next_page_token"})


def decode(blob: bytes) -> Any:  # noqa: ANN401 - decoded JSON
    """Response bytes with exact decimals; NaN/Infinity refused."""

    def refuse(text: str) -> object:
        raise CorporateActionParseError("INVALID_JSON", f"constant {text}")

    try:
        return json.loads(blob, parse_float=Decimal, parse_constant=refuse)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CorporateActionParseError("INVALID_JSON", str(exc)) from None


def _canon(value: object) -> object:
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, dict):
        return {k: _canon(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_canon(v) for v in value]
    return value


def content_digest(item: Mapping[str, Any]) -> str:
    blob = json.dumps(_canon(dict(item)), sort_keys=True, separators=(",", ":"))
    return sha256(blob.encode())


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    provider_id: str
    action_type: str
    category: EventCategory
    symbols: tuple[str, ...]
    """Universe symbols the event names (``symbol`` for verified shapes; any
    ``*symbol`` field for unverified ones). Empty = unattributed."""
    effective_date: date
    dates: tuple[tuple[DateRole, date], ...]
    raw: Mapping[str, Any] = field(repr=False, compare=False)
    content_sha256: str = ""
    shape_verified: bool = False

    def date_of(self, role: DateRole) -> date | None:
        return dict(self.dates).get(role)


def _iso_day(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise CorporateActionParseError("INVALID_DATE", f"{name} must be a string")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CorporateActionParseError("INVALID_DATE", f"{name}={value!r}") from None


def _event(action_type: str, item: object, universe: frozenset[str]) -> ProviderEvent:
    spec = ACTION_TYPES[action_type]
    if not isinstance(item, dict):
        raise CorporateActionParseError("INVALID_EVENT", f"{action_type} item is not an object")
    pid = item.get("id")
    if not isinstance(pid, str) or not pid.strip():
        raise CorporateActionParseError("INVALID_EVENT", f"{action_type} item without id")
    dates = tuple((role, _iso_day(item[role.value], role.value))
                  for role in DateRole if item.get(role.value) is not None)  # fmt: skip
    found = dict(dates)
    if spec.category is EventCategory.OTHER:
        effective = found.get(DateRole.EX_DATE)
        if effective is None and item.get("effective_date") is not None:
            effective = _iso_day(item["effective_date"], "effective_date")
    else:
        effective = found.get(DateRole.EX_DATE)
    if effective is None:
        raise CorporateActionParseError(
            "UNRECOGNIZED_EVENT_SHAPE", f"{action_type} {pid}: no ex_date/effective_date"
        )
    verified = action_type in VERIFIED_SHAPES
    if verified:
        sym = item.get("symbol")
        if not isinstance(sym, str):
            raise CorporateActionParseError("UNRECOGNIZED_EVENT_SHAPE",
                                            f"{action_type} {pid}: no symbol")  # fmt: skip
        named: tuple[str, ...] = (sym,)
    else:
        named = tuple(v for k, v in sorted(item.items())
                      if k.endswith("symbol") and isinstance(v, str))  # fmt: skip
    symbols = tuple(sorted({s for s in named if s in universe}))
    return ProviderEvent(pid, action_type, spec.category, symbols, effective, dates, item,
                         content_digest(item), verified)  # fmt: skip


@dataclass(frozen=True, slots=True)
class CorporateActionPage:
    events: tuple[ProviderEvent, ...]
    next_page_token: str | None


def parse_page(blob: bytes, universe: Sequence[str]) -> CorporateActionPage:
    """One ``GET /v1/corporate-actions`` body, with the shape seen by the 16.16D probe:
    ``{"corporate_actions": {<list name>: [...]}, "next_page_token": str|null}``.
    Unknown keys or list names, a malformed event or a bad token fail the parse."""
    doc = decode(blob)
    if not isinstance(doc, dict) or set(doc) != _TOP_KEYS:
        raise CorporateActionParseError("UNEXPECTED_SHAPE", "top-level keys")
    token = doc["next_page_token"]
    if token is not None and (not isinstance(token, str) or not token):
        raise CorporateActionParseError("INVALID_PAGE_TOKEN")
    lists = doc["corporate_actions"]
    if not isinstance(lists, dict):
        raise CorporateActionParseError("UNEXPECTED_SHAPE", "corporate_actions")
    members = frozenset(universe)
    events: list[ProviderEvent] = []
    for name in sorted(lists):
        if name not in ACTION_TYPES:
            raise CorporateActionParseError("UNKNOWN_ACTION_TYPE", name)
        items = lists[name]
        if not isinstance(items, list):
            raise CorporateActionParseError("UNEXPECTED_SHAPE", f"{name} is not a list")
        events.extend(_event(name, item, members) for item in items)
    return CorporateActionPage(tuple(events), token)


# ===================================================================== windows


class ImportIssue(StrEnum):
    DATE_SEMANTICS_UNVERIFIED = "DATE_SEMANTICS_UNVERIFIED"
    FILTER_ROLE_EXCLUDED = "FILTER_ROLE_EXCLUDED"
    """A returned event's date for a candidate role lies outside the request window,
    so that role cannot be the filter (information; narrows the semantics)."""
    PAD_MARGIN_EXCEEDED = "PAD_MARGIN_EXCEEDED"
    FILTER_DATE_MISSING = "FILTER_DATE_MISSING"
    PAD_CROSS_CHECK_MISSING = "PAD_CROSS_CHECK_MISSING"
    DUPLICATE_EVENT_CONFLICT = "DUPLICATE_EVENT_CONFLICT"
    AMBIGUOUS_DUPLICATE = "AMBIGUOUS_DUPLICATE"
    UNATTRIBUTED_EVENT = "UNATTRIBUTED_EVENT"
    UNVERIFIED_EVENT_SHAPE = "UNVERIFIED_EVENT_SHAPE"
    EVENT_INVALID = "EVENT_INVALID"
    COVERAGE_GAP = "COVERAGE_GAP"


#: Findings that withhold the PROVIDER_REPORTED claim for EVERY symbol of their list.
_GLOBAL_BLOCKS: Final = frozenset({
    ImportIssue.DATE_SEMANTICS_UNVERIFIED, ImportIssue.PAD_MARGIN_EXCEEDED,
    ImportIssue.FILTER_DATE_MISSING, ImportIssue.PAD_CROSS_CHECK_MISSING,
    ImportIssue.UNATTRIBUTED_EVENT, ImportIssue.COVERAGE_GAP,
})  # fmt: skip
#: Findings that withhold the claim for their own symbol and list.
_SYMBOL_BLOCKS: Final = frozenset({
    ImportIssue.DUPLICATE_EVENT_CONFLICT, ImportIssue.AMBIGUOUS_DUPLICATE,
    ImportIssue.EVENT_INVALID,
})  # fmt: skip


@dataclass(frozen=True, slots=True, order=True)
class ImportFinding:
    issue: ImportIssue
    list_name: str
    """``splits``, ``dividends``, ``other_events`` or ``*`` (all lists)."""
    symbol: str = "*"
    detail: str = ""
    role: str = ""
    """The date role a padding or exclusion finding is about ("" otherwise)."""
    action_type: str = ""
    """The event's response list name, for findings about one event ("" otherwise)."""

    def to_dict(self) -> dict[str, str]:
        out = {"issue": self.issue.value, "list": self.list_name, "symbol": self.symbol,
               "detail": self.detail}  # fmt: skip
        if self.role:
            out["role"] = self.role
        if self.action_type:
            out["action_type"] = self.action_type
        return out


@dataclass(frozen=True, slots=True)
class WindowSelection:
    batch_id: str
    window: CorporateActionWindow
    in_coverage: tuple[ProviderEvent, ...]
    pad_zone: tuple[ProviderEvent, ...]
    findings: tuple[ImportFinding, ...]


def select_window(batch_id: str, window: CorporateActionWindow,
                  events: Sequence[ProviderEvent], semantics: FilterSemantics, *,
                  margin_days: int = CA_PAD_MARGIN_DAYS) -> WindowSelection:  # fmt: skip
    """Classify one batch's events by effective date and test the padding assumption:
    for each role that may be the filter, ``filter date - effective date`` must lie
    within ``[-pad_before + margin, pad_after - margin]``."""
    inside: list[ProviderEvent] = []
    pad: list[ProviderEvent] = []
    findings: list[ImportFinding] = []
    lo = -window.pad_before_days + margin_days
    hi = window.pad_after_days - margin_days
    for ev in events:
        target = inside if window.coverage_start <= ev.effective_date <= window.coverage_end \
            else pad  # fmt: skip
        target.append(ev)
        name = ev.category.value
        sym = ",".join(ev.symbols) or "*"
        if not ev.shape_verified:
            findings.append(ImportFinding(ImportIssue.UNVERIFIED_EVENT_SHAPE, name, sym,
                                          f"{ev.action_type} {ev.provider_id}"))  # fmt: skip
        if not ev.symbols:
            findings.append(ImportFinding(ImportIssue.UNATTRIBUTED_EVENT, name, "*",
                                          f"{ev.action_type} {ev.provider_id}"))  # fmt: skip
        for role in DateRole:
            d = ev.date_of(role)
            if d is not None and not window.request_start <= d <= window.request_end:
                findings.append(
                    ImportFinding(
                        ImportIssue.FILTER_ROLE_EXCLUDED,
                        "*",
                        "*",
                        f"{role.value} {d} of {ev.provider_id}",
                        role=role.value,
                        action_type=ev.action_type,
                    )
                )
        if target is pad:
            continue
        for role in semantics.roles_for(ev.action_type):
            d = ev.date_of(role)
            if d is None:
                findings.append(
                    ImportFinding(
                        ImportIssue.FILTER_DATE_MISSING,
                        name,
                        sym,
                        f"{role.value} of {ev.provider_id}",
                        role=role.value,
                        action_type=ev.action_type,
                    )
                )
                continue
            lag = (d - ev.effective_date).days
            if not lo <= lag <= hi:
                findings.append(
                    ImportFinding(
                        ImportIssue.PAD_MARGIN_EXCEEDED,
                        name,
                        sym,
                        f"{role.value} lag {lag} d outside [{lo}, {hi}] ({ev.provider_id})",
                        role=role.value,
                        action_type=ev.action_type,
                    )
                )
    return WindowSelection(batch_id, window, tuple(inside), tuple(pad), tuple(findings))


def narrow_semantics(prior: FilterSemantics,
                     selections: Sequence[WindowSelection]) -> FilterSemantics:  # fmt: skip
    """Exclude every candidate role contradicted by a returned event (its date outside
    the request window). Never upgrades the status to LIVE_VERIFIED."""
    excluded = {f.role for s in selections for f in s.findings
                if f.issue is ImportIssue.FILTER_ROLE_EXCLUDED}  # fmt: skip
    left = tuple(r for r in prior.candidates if r.value not in excluded)
    if prior.status is SemanticsStatus.LIVE_VERIFIED and left == prior.candidates:
        fallback = tuple(r for r in prior.fallback if r.value not in excluded)
        if prior.fallback and not fallback:
            return FilterSemantics(SemanticsStatus.INCONCLUSIVE, (),
                                   "every fallback candidate excluded")  # fmt: skip
        return replace(prior, fallback=fallback)
    if not left:
        return FilterSemantics(SemanticsStatus.INCONCLUSIVE, (), "every candidate excluded")
    status = (SemanticsStatus.INCONCLUSIVE if prior.status is SemanticsStatus.LIVE_VERIFIED
              else SemanticsStatus.CONSISTENT_WITH_OBSERVATIONS)  # fmt: skip
    return FilterSemantics(status, left, f"{prior.evidence}; excluded {sorted(excluded)}")


# ===================================================================== merge


@dataclass(frozen=True, slots=True)
class CorporateActionImport:
    """Records for every universe symbol plus every finding (kept in the manifest)."""

    records: Mapping[str, CorporateActionRecord]
    findings: tuple[ImportFinding, ...]
    semantics: FilterSemantics
    claims: Mapping[str, Mapping[str, tuple[str, ...]]]
    """symbol -> list -> evidence kinds claimed (empty: no claim, with the reasons in
    ``findings``)."""

    def blocked(self, symbol: str, list_name: str) -> tuple[ImportFinding, ...]:
        return tuple(f for f in self.findings if _blocks(f, symbol, list_name))


def _blocks(f: ImportFinding, symbol: str, list_name: str) -> bool:
    lists = f.list_name in ("*", list_name)
    if f.issue in _GLOBAL_BLOCKS:
        return lists
    return f.issue in _SYMBOL_BLOCKS and lists and symbol in f.symbol.split(",")


def _merge(selections: Sequence[WindowSelection]) -> tuple[list[ProviderEvent],
                                                           list[ImportFinding]]:  # fmt: skip
    findings: list[ImportFinding] = []
    by_id: dict[str, ProviderEvent] = {}
    conflicted: set[str] = set()
    for sel in selections:
        for ev in (*sel.in_coverage, *sel.pad_zone):
            seen = by_id.get(ev.provider_id)
            if seen is None:
                by_id[ev.provider_id] = ev
            elif seen.content_sha256 != ev.content_sha256 and ev.provider_id not in conflicted:
                conflicted.add(ev.provider_id)
                findings.append(
                    ImportFinding(
                        ImportIssue.DUPLICATE_EVENT_CONFLICT,
                        ev.category.value,
                        ",".join(ev.symbols) or "*",
                        f"id {ev.provider_id} differs between pages",
                    )
                )
    kept = [by_id[i] for i in sorted({e.provider_id for s in selections for e in s.in_coverage})
            if i not in conflicted]  # fmt: skip
    natural: defaultdict[tuple[str, str, date], list[str]] = defaultdict(list)
    for ev in kept:
        for sym in ev.symbols:
            natural[(sym, ev.action_type, ev.effective_date)].append(ev.provider_id)
    for (sym, kind, day), ids in sorted(natural.items()):
        if len(ids) > 1:
            findings.append(ImportFinding(ImportIssue.AMBIGUOUS_DUPLICATE,
                                          ACTION_TYPES[kind].category.value, sym,
                                          f"{kind} {day}: ids {sorted(ids)}"))  # fmt: skip
    # pad-zone observations must be confirmed by the batch that owns their date
    owned = {e.provider_id for s in selections for e in s.in_coverage}
    for sel in selections:
        for ev in sel.pad_zone:
            owner = next((o for o in selections if o.window.coverage_start
                          <= ev.effective_date <= o.window.coverage_end), None)  # fmt: skip
            if owner is not None and ev.provider_id not in owned:
                findings.append(
                    ImportFinding(
                        ImportIssue.PAD_CROSS_CHECK_MISSING,
                        ev.category.value,
                        ",".join(ev.symbols) or "*",
                        f"{ev.provider_id} seen by {sel.batch_id}, not by {owner.batch_id}",
                    )
                )
    return kept, findings


def _contiguous_end(selections: Sequence[WindowSelection], start: date) -> date | None:
    end: date | None = None
    for sel in sorted(selections, key=lambda s: s.window.coverage_start):
        nxt = start if end is None else date.fromordinal(end.toordinal() + 1)
        if sel.window.coverage_start != nxt:
            break
        end = sel.window.coverage_end
    return end


def _attestations_ok(attestations: Mapping[tuple[str, str], Sequence[Evidence]]) -> None:
    for (sym, name), items in attestations.items():
        if name not in LISTS:
            raise ValueError(f"unknown list {name!r} for {sym}")
        for e in items:
            if e.kind not in (EvidenceKind.INDEPENDENT_SOURCE, EvidenceKind.OPERATOR_REVIEW):
                raise ValueError(f"{sym} {name}: attestations must be INDEPENDENT_SOURCE or "
                                 "OPERATOR_REVIEW")  # fmt: skip


@dataclass(slots=True)
class _Lists:
    splits: defaultdict[str, list[SplitEvent]] = field(default_factory=lambda: defaultdict(list))
    dividends: defaultdict[str, list[CashDividend]] = field(
        default_factory=lambda: defaultdict(list)
    )
    kinds: defaultdict[str, list[tuple[date, DistributionKind]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    others: defaultdict[str, list[OtherEvent]] = field(default_factory=lambda: defaultdict(list))

    def add(self, ev: ProviderEvent, sym: str) -> None:
        """Map one provider event; raises DataContractError on an unusable one."""
        source = f"{PROVIDER_SOURCE}#{ev.provider_id}"
        if ev.action_type in ("forward_splits", "reverse_splits"):
            self.splits[sym].append(SplitEvent(sym, ev.effective_date, ev.raw["new_rate"],
                                               ev.raw["old_rate"], source))  # fmt: skip
            return
        if ev.category is EventCategory.DIVIDEND:
            pay = ev.date_of(DateRole.PAYABLE_DATE)
            rate = ev.raw.get("rate")
            if pay is None or isinstance(rate, bool) or not isinstance(rate, int | Decimal):
                raise DataContractError("payable_date and a numeric rate are required")
            self.dividends[sym].append(CashDividend(sym, ev.effective_date, pay,
                                                    Decimal(rate), source))  # fmt: skip
            special = ev.raw.get("special") is True
            self.kinds[sym].append((ev.effective_date,
                                    DistributionKind.SPECIAL_CASH_DIVIDEND if special
                                    else DistributionKind.CASH_DIVIDEND))  # fmt: skip
            return
        kind = ACTION_TYPES[ev.action_type].other
        if kind is None:  # pragma: no cover - table invariant
            raise DataContractError(f"{ev.action_type} has no event kind")
        self.others[sym].append(OtherEvent(kind, ev.effective_date,
                                           f"{ev.action_type} id={ev.provider_id}",
                                           PROVIDER_SOURCE))  # fmt: skip

    def record(self, sym: str, covs: Mapping[str, Coverage | None]) -> CorporateActionRecord:
        return CorporateActionRecord(
            sym, tuple(sorted(self.splits[sym], key=lambda s: s.effective_session)), (),
            tuple(sorted(self.dividends[sym], key=lambda d: d.ex_session)),
            tuple(self.kinds[sym]),
            tuple(sorted(self.others[sym], key=lambda e: e.effective_session)),
            covs["splits"], covs["dividends"], covs["other_events"],
        )  # fmt: skip


def _still_relevant(f: ImportFinding, semantics: FilterSemantics) -> bool:
    """Drop margin findings about a role the observations have since excluded."""
    if f.issue is ImportIssue.FILTER_ROLE_EXCLUDED:
        return False
    if f.issue in (ImportIssue.PAD_MARGIN_EXCEEDED, ImportIssue.FILTER_DATE_MISSING):
        return f.role in {r.value for r in semantics.roles_for(f.action_type)}
    return True


def build_records(
    selections: Sequence[WindowSelection],
    *,
    symbols: Sequence[str],
    target: tuple[date, date],
    semantics: FilterSemantics,
    attestations: Mapping[tuple[str, str], Sequence[Evidence]] | None = None,
) -> CorporateActionImport:
    """Merge every batch's selection into one record per symbol. ``target`` is the
    coverage the dataset needs (warm-up start .. research end). A claim always spans
    ``target``; it carries PROVIDER_REPORTED evidence only when nothing blocks it, plus
    any explicit attestations."""
    attest = dict(attestations or {})
    _attestations_ok(attest)
    semantics = narrow_semantics(semantics, selections)
    kept, findings = _merge(selections)
    findings += [f for s in selections for f in s.findings if _still_relevant(f, semantics)]
    for name in LISTS:
        unverified = sorted(
            t for t, spec in ACTION_TYPES.items()
            if spec.category.value == name and semantics.role_for(t) is None
        )  # fmt: skip
        if unverified:
            findings.append(
                ImportFinding(
                    ImportIssue.DATE_SEMANTICS_UNVERIFIED,
                    name,
                    "*",
                    f"{semantics.status.value}, verified for {list(semantics.scope)}; "
                    f"unverified for {unverified}: {semantics.evidence}",
                )
            )
    end = _contiguous_end(selections, target[0])
    if end is None or end < target[1]:
        findings.append(
            ImportFinding(
                ImportIssue.COVERAGE_GAP,
                "*",
                "*",
                f"slices cover {target[0]}..{end}, need ..{target[1]}",
            )
        )
    lists = _Lists()
    for ev in kept:
        for sym in ev.symbols:
            if sym not in symbols:
                continue
            try:
                lists.add(ev, sym)
            except (DataContractError, KeyError, TypeError) as exc:
                findings.append(ImportFinding(ImportIssue.EVENT_INVALID, ev.category.value,
                                              sym, f"{ev.provider_id}: {exc}"))  # fmt: skip
    ordered = tuple(sorted(set(findings)))
    refs = ",".join(sorted(s.batch_id for s in selections))
    records: dict[str, CorporateActionRecord] = {}
    claims: dict[str, dict[str, tuple[str, ...]]] = {}
    for sym in symbols:
        covs: dict[str, Coverage | None] = {}
        claims[sym] = {}
        for name in LISTS:
            evidence: list[Evidence] = []
            if not any(_blocks(f, sym, name) for f in ordered):
                evidence.append(Evidence(EvidenceKind.PROVIDER_REPORTED, PROVIDER_SOURCE,
                                         f"batches:{refs}"))  # fmt: skip
            evidence += attest.get((sym, name), ())
            covs[name] = Coverage(target[0], target[1], tuple(evidence)) if evidence else None
            claims[sym][name] = tuple(e.kind.value for e in evidence)
        records[sym] = lists.record(sym, covs)
    return CorporateActionImport(records, ordered, semantics, claims)


__all__ = [
    "ACTION_TYPES",
    "ANY_LISTED_DATE",
    "LISTS",
    "OBSERVED_16_16D",
    "PROVIDER_SOURCE",
    "VERIFIED_SHAPES",
    "CorporateActionImport",
    "CorporateActionPage",
    "CorporateActionParseError",
    "DateRole",
    "EventCategory",
    "FilterSemantics",
    "ImportFinding",
    "ImportIssue",
    "ProviderEvent",
    "SemanticsStatus",
    "WindowSelection",
    "build_records",
    "content_digest",
    "decode",
    "discriminated_types",
    "discriminating_events",
    "evaluate_semantics_probe",
    "expected_pattern",
    "hypothesis_table",
    "narrow_semantics",
    "parse_page",
    "select_window",
]
