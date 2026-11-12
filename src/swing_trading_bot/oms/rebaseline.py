"""Audited, operator-controlled rebaseline (ARCHITECTURE §8.5, §12.18).

Purpose: re-anchor broker-side cash reconciliation after an event outside the bot's
control (e.g. a paper-account reset or a manual deposit). It is never a way to make an
unexplained difference disappear silently: it never changes virtual cash, cost basis,
positions, fills or cash entries, and it never clears the persistent halt.

Two phases, both refused — and the refusal recorded in ``rebaseline_events`` — unless
every precondition holds:

1. :meth:`Rebaseliner.propose` (operator + reason). Runs a **fresh reconciliation**,
   then requires: a persistent halt is active; no working or unresolved order in the
   ledger and no open order at the broker; complete evidence (no INCONCLUSIVE finding);
   broker positions equal ledger positions; every MISMATCH is of a kind a rebaseline can
   acknowledge (unexplained cash, unexplained cash activity, unexplained/external
   executions already closed out) and there is something to acknowledge. It stores an
   immutable ``rebaseline_requests`` row (``PROPOSED``) with the diff and evidence, the
   broker-state hash, the economic ledger fingerprint and the **exact unexplained
   amount** = broker cash - baseline cash - Σ attributable and external activity since
   the baseline (i.e. the residual plus every unexplained activity).
2. :meth:`Rebaseliner.confirm` (request id + the amount typed by the operator).
   Refuses a request that is not ``PROPOSED`` (an ``APPROVED`` one with the same amount
   is an idempotent duplicate that returns the original result), older than
   ``preview_max_age`` (→ ``EXPIRED``), or whose typed amount differs (stays
   ``PROPOSED`` so a typo can be corrected). It then runs **another fresh
   reconciliation** and refuses (→ ``EXPIRED``) if the broker-state hash, the economic
   ledger fingerprint or the unexplained amount changed, or if any precondition no
   longer holds. Only then, in ONE transaction: the active baseline is marked
   superseded (kept forever), the new baseline (broker cash at this observation) is
   inserted linked to the request, the request becomes ``APPROVED`` with the
   acknowledged amount, the halt is re-recorded as ``REBASELINE`` (it stays halted), and
   an ``APPROVED`` event is written.

Trading resumes only through the normal path: a later CLEAN reconciliation against the
new baseline and a separate operator resume
(:func:`~swing_trading_bot.oms.reconciler.resume_after_clean_reconciliation`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final

from swing_trading_bot.domain.enums import HaltReason
from swing_trading_bot.domain.money import ZERO
from swing_trading_bot.ledger.acknowledgements import ACKNOWLEDGED_ACTIVITY_SQL
from swing_trading_bot.ledger.baselines import (
    BrokerBaseline,
    BrokerBaselinesRepository,
    cash_residual,
)
from swing_trading_bot.ledger.codec import dec_to_db, ts_from_db, ts_to_db
from swing_trading_bot.ledger.db import Database, Session, Tx, assert_no_transaction
from swing_trading_bot.ledger.portfolio import VirtualPortfolio
from swing_trading_bot.ledger.repositories import (
    OrdersRepository,
    TradingStateRepository,
    canonical_json,
)
from swing_trading_bot.ledger.state_fingerprint import ledger_fingerprint
from swing_trading_bot.oms.order_manager import CrashHook
from swing_trading_bot.oms.reconciler import (
    Category,
    Reconciler,
    ReconciliationReport,
    Severity,
)

ACKNOWLEDGEABLE: Final = frozenset(
    {
        "CASH_UNEXPLAINED",
        "UNEXPLAINED_CASH_ACTIVITY",
        "DIVIDEND_ENTITLEMENT_UNVERIFIED",
        "UNEXPLAINED_FILL",
        "EXTERNAL_ORDER",
    }
)
"""MISMATCH kinds a rebaseline may acknowledge. Anything else (ledger invariants, order
conflicts, out-of-order bookings, account identity, positions) needs its own remedy."""

ECONOMIC_TABLES: Final = (
    "broker_activities",
    "broker_baselines",
    "cash_entries",
    "cash_reservations",
    "fills",
    "ledger_adjustments",
    "orders",
    "positions",
    "settlement_resolutions",
)
"""Ledger state a preview is bound to. Audit tables (runs, findings, halt and order
events) are excluded: the confirming reconciliation itself appends to them."""

PREVIEW_MAX_AGE: Final = timedelta(minutes=15)


class RebaselineError(RuntimeError):
    pass


class Decision(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    DUPLICATE = "DUPLICATE"
    REFUSED = "REFUSED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class AckItem:
    """One broker event a rebaseline would acknowledge, bound to its exact payload."""

    kind: str  # "ACTIVITY" | "ORDER"
    ref_id: str
    payload: str
    amount: Decimal | None
    in_baseline: bool
    """True when the activity was ingested before the active baseline was taken: its cash
    is already inside the baseline cash and is NOT part of the net difference."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref_id": self.ref_id,
            "payload": self.payload,
            "amount": None if self.amount is None else str(self.amount),
            "in_baseline": self.in_baseline,
        }


@dataclass(frozen=True, slots=True)
class Preview:
    """The cash equation of a rebaseline (§12.19):

    * ``residual`` R = broker cash - (baseline cash + Σ net of every activity ingested
      after the baseline) — cash that moved with no activity at all. Activities are
      already inside that sum, so their amounts are never in R.
    * each unexplained activity ingested after the baseline moved broker cash *with* an
      activity: its amount is counted once, as its own item;
    * an unexplained activity ingested before the baseline is already inside the
      baseline cash: listed (it still needs acknowledging) but not counted;
    * ``net`` N = R + Σ counted activity amounts — the amount the operator must type.
    """

    residual: Decimal
    activities: tuple[AckItem, ...]
    orders: tuple[AckItem, ...]

    @property
    def net(self) -> Decimal:
        counted = (a.amount for a in self.activities if a.amount is not None and not a.in_baseline)
        return self.residual + sum(counted, ZERO)

    @property
    def empty(self) -> bool:
        return self.residual == ZERO and not self.activities and not self.orders

    def as_dict(self) -> dict[str, Any]:
        return {
            "residual": str(self.residual),
            "net": str(self.net),
            "activities": [a.as_dict() for a in self.activities],
            "orders": [o.as_dict() for o in self.orders],
        }


@dataclass(frozen=True, slots=True)
class RebaselineOutcome:
    decision: Decision
    request_id: int | None
    reasons: tuple[str, ...] = ()
    unexplained: Decimal | None = None
    new_baseline_id: int | None = None
    reconciliation_run_id: int | None = None
    preview: Preview | None = None


class Rebaseliner:
    def __init__(
        self,
        db: Database,
        portfolio: VirtualPortfolio,
        reconciler: Reconciler,
        *,
        preview_max_age: timedelta = PREVIEW_MAX_AGE,
        crash: CrashHook | None = None,
    ) -> None:
        self._db = db
        self._pf = portfolio
        self._recon = reconciler
        self._max_age = preview_max_age
        self._crash = crash or (lambda _point: None)

    # ============================================================ propose
    def propose(self, *, actor: str, reason: str, now: datetime) -> RebaselineOutcome:
        _require_text(actor=actor, reason=reason)
        assert_no_transaction("rebaseline propose")
        if not self._halted():
            return self._refuse_proposal(actor, now, ["trading is not persistently halted"])
        report = self._recon.run(now=now)
        with self._db.read() as s:
            problems, preview, baseline = self._assess(s, report)
            economic = ledger_fingerprint(s, ECONOMIC_TABLES)
        if problems or baseline is None or preview is None:
            return self._refuse_proposal(
                actor, now, problems or ["no active baseline"], run_id=report.run_id
            )
        diff = {
            "reconciliation_run_id": report.run_id,
            "observed_at": report.evidence["observed_at"],
            "broker_cash": report.evidence["account"]["cash"],
            "baseline_id": baseline.id,
            "baseline_cash": str(baseline.cash),
            "findings": [[f.category.value, f.code, f.subject] for f in report.findings],
            "ledger_fingerprint": economic,
            "preview": preview.as_dict(),
        }
        with self._db.transaction() as tx:
            request_id = tx.insert(
                "INSERT INTO rebaseline_requests (created_at, reason, actor, diff_json, "
                "broker_snapshot_hash, unexplained_cash, unclassified_activities_json, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'PROPOSED')",
                (
                    ts_to_db(now),
                    reason,
                    actor,
                    canonical_json(diff),
                    report.evidence["broker_snapshot_hash"],
                    dec_to_db(preview.net),
                    canonical_json([a.as_dict() for a in (*preview.activities, *preview.orders)]),
                ),
            )
            _event(
                tx,
                request_id,
                now,
                "PROPOSED",
                actor,
                residual=str(preview.residual),
                net=str(preview.net),
                activities=[a.ref_id for a in preview.activities],
                orders=[o.ref_id for o in preview.orders],
                run_id=report.run_id,
            )
        return RebaselineOutcome(
            Decision.PROPOSED,
            request_id,
            unexplained=preview.net,
            reconciliation_run_id=report.run_id,
            preview=preview,
        )

    # ============================================================ confirm
    def confirm(  # noqa: PLR0911 - each refusal is a distinct, audited outcome
        self, request_id: int, *, acknowledged: str, actor: str, reason: str, now: datetime
    ) -> RebaselineOutcome:
        _require_text(actor=actor, reason=reason)
        assert_no_transaction("rebaseline confirm")
        typed = _parse_amount(acknowledged)
        with self._db.read() as s:
            req = _request(s, request_id)
        if req is None:
            raise RebaselineError(f"rebaseline request {request_id} does not exist")
        if req["status"] == "APPROVED":
            return self._duplicate(req, typed, actor, now)
        if req["status"] != "PROPOSED":
            return self._refuse(request_id, actor, now, [f"request is {req['status']}"])
        if now - ts_from_db(req["created_at"]) > self._max_age or now < ts_from_db(
            req["created_at"]
        ):
            return self._expire(request_id, actor, now, ["preview is stale"])
        if typed is None or typed != Decimal(req["unexplained_cash"]):
            return self._refuse(
                request_id,
                actor,
                now,
                [f"typed amount {acknowledged!r} does not equal the unexplained amount"],
            )
        if not self._halted():
            return self._expire(request_id, actor, now, ["trading is not persistently halted"])
        report = self._recon.run(now=now)
        diff = json.loads(req["diff_json"])
        with self._db.read() as s:
            problems, preview, baseline = self._assess(s, report)
            economic = ledger_fingerprint(s, ECONOMIC_TABLES)
        if report.evidence["broker_snapshot_hash"] != req["broker_snapshot_hash"]:
            problems.append("broker evidence changed since the preview")
        if economic != diff["ledger_fingerprint"]:
            problems.append("ledger state changed since the preview")
        if preview is None or preview.as_dict() != diff["preview"]:
            problems.append("acknowledged events or cash difference changed since the preview")
        if baseline is None or baseline.id != diff["baseline_id"]:
            problems.append("active baseline changed since the preview")
        if problems or preview is None or baseline is None:
            return self._expire(request_id, actor, now, problems, run_id=report.run_id)
        return self._approve(req, baseline, report, preview, typed, actor, reason, now)

    def _approve(  # noqa: PLR0917 - the immutable preview and the fresh evidence
        self,
        req: dict[str, Any],
        baseline: BrokerBaseline,
        report: ReconciliationReport,
        preview: Preview,
        typed: Decimal,
        actor: str,
        reason: str,
        now: datetime,
    ) -> RebaselineOutcome:
        request_id = int(req["id"])
        cash = Decimal(report.evidence["account"]["cash"])
        equity = cash + Decimal(report.evidence["positions_cost"] or "0")
        taken_at = datetime.fromisoformat(report.evidence["observed_at"])
        with self._db.transaction() as tx:
            n = tx.execute(
                "UPDATE rebaseline_requests SET status = 'APPROVED', acknowledged_amount = ?, "
                "approved_at = ?, approval_reason = ? WHERE id = ? AND status = 'PROPOSED'",
                (dec_to_db(typed), ts_to_db(now), reason, request_id),
            )
            if n != 1:
                raise RebaselineError("request was decided concurrently")
            new_id = BrokerBaselinesRepository().replace_for_rebaseline(
                tx,
                portfolio_id=baseline.portfolio_id,
                request_id=request_id,
                taken_at=taken_at,
                cash=cash,
                equity=equity,
                account_config=baseline.account_config,
            )
            for item in (*preview.activities, *preview.orders):
                tx.insert(
                    "INSERT INTO rebaseline_acknowledgements (request_id, kind, ref_id, payload, "
                    "amount, in_baseline) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        request_id,
                        item.kind,
                        item.ref_id,
                        item.payload,
                        None if item.amount is None else dec_to_db(item.amount),
                        1 if item.in_baseline else 0,
                    ),
                )
            TradingStateRepository().halt(
                tx,
                HaltReason.REBASELINE,
                actor=actor,
                at=now,
                detail=f"rebaseline request {request_id} approved: baseline {baseline.id} -> "
                f"{new_id}, acknowledged {typed}; resume needs a clean reconciliation",
                reconciliation_run_id=report.run_id,
            )
            _event(
                tx,
                request_id,
                now,
                "APPROVED",
                actor,
                new_baseline_id=new_id,
                old_baseline_id=baseline.id,
                acknowledged=str(typed),
                run_id=report.run_id,
            )
            self._crash("rebaseline:before_commit")
        self._crash("rebaseline:after_commit")
        return RebaselineOutcome(
            Decision.APPROVED,
            request_id,
            unexplained=typed,
            new_baseline_id=new_id,
            reconciliation_run_id=report.run_id,
            preview=preview,
        )

    def _duplicate(
        self, req: dict[str, Any], typed: Decimal | None, actor: str, now: datetime
    ) -> RebaselineOutcome:
        request_id = int(req["id"])
        if typed is None or typed != Decimal(req["acknowledged_amount"]):
            return self._refuse(
                request_id, actor, now, ["request already approved with a different amount"]
            )
        with self._db.transaction() as tx:
            row = tx.fetch_one(
                "SELECT id FROM broker_baselines WHERE rebaseline_request_id = ?", (request_id,)
            )
            new_id = None if row is None else int(row[0])
            _event(tx, request_id, now, "DUPLICATE_CONFIRM", actor, new_baseline_id=new_id)
        return RebaselineOutcome(
            Decision.DUPLICATE, request_id, unexplained=typed, new_baseline_id=new_id
        )

    # ============================================================ helpers
    def _assess(
        self, s: Session, report: ReconciliationReport
    ) -> tuple[list[str], Preview | None, BrokerBaseline | None]:
        problems: list[str] = []
        pid = self._pf.portfolio(s).id or 0
        baseline = BrokerBaselinesRepository().active(s, pid)
        inconclusive = sorted(
            {f.code for f in report.findings if f.severity is Severity.INCONCLUSIVE}
        )
        if inconclusive:
            problems.append("evidence incomplete: " + ",".join(inconclusive))
        positions = sorted({f.subject for f in report.findings if f.category is Category.POSITION})
        if positions:
            problems.append("broker positions differ from the ledger: " + ",".join(positions))
        other = sorted(
            {
                f.code
                for f in report.findings
                if f.severity is Severity.MISMATCH
                and f.code not in ACKNOWLEDGEABLE
                and f.category is not Category.POSITION
            }
        )
        if other:
            problems.append("mismatches a rebaseline cannot acknowledge: " + ",".join(other))
        if OrdersRepository().list_working(s):
            problems.append("working or unresolved orders exist in the ledger")
        if report.evidence.get("open_orders"):
            problems.append("open orders exist at the broker")
        account = report.evidence.get("account")
        if baseline is None or account is None:
            return [*problems, "no baseline or no account evidence"], None, baseline
        residual = cash_residual(s, baseline, Decimal(account["cash"]))
        cutoff = ts_to_db(baseline.taken_at)
        # Every unexplained activity not yet acknowledged, whatever its timestamp.
        cols = "activity_id, raw_json, net_amount, ingested_at <= ?"
        where = f"attribution = 'UNEXPLAINED' AND NOT {ACKNOWLEDGED_ACTIVITY_SQL}"
        sql = f"SELECT {cols} FROM broker_activities WHERE {where} ORDER BY activity_id"  # noqa: S608
        activities = tuple(
            AckItem(
                "ACTIVITY",
                str(aid),
                str(raw),
                None if amount is None else Decimal(str(amount)),
                bool(in_base),
            )
            for aid, raw, amount, in_base in s.fetch_all(sql, (cutoff,))
        )
        orders = tuple(
            sorted(
                (
                    AckItem("ORDER", f.subject, f.detail_dict()["payload"], None, in_baseline=False)
                    for f in report.findings
                    if f.code == "EXTERNAL_ORDER"
                ),
                key=lambda o: o.ref_id,
            )
        )
        preview = Preview(residual, activities, orders)
        if preview.empty:
            problems.append("nothing to acknowledge: broker cash and events are fully explained")
        return problems, preview, baseline

    def _halted(self) -> bool:
        with self._db.read() as s:
            return TradingStateRepository().get(s).halted

    def _refuse_proposal(
        self, actor: str, now: datetime, reasons: list[str], *, run_id: int | None = None
    ) -> RebaselineOutcome:
        with self._db.transaction() as tx:
            _event(tx, None, now, "PROPOSE_REFUSED", actor, reasons=reasons, run_id=run_id)
        return RebaselineOutcome(
            Decision.REFUSED, None, tuple(reasons), reconciliation_run_id=run_id
        )

    def _refuse(
        self, request_id: int, actor: str, now: datetime, reasons: list[str]
    ) -> RebaselineOutcome:
        with self._db.transaction() as tx:
            _event(tx, request_id, now, "CONFIRM_REFUSED", actor, reasons=reasons)
        return RebaselineOutcome(Decision.REFUSED, request_id, tuple(reasons))

    def _expire(
        self,
        request_id: int,
        actor: str,
        now: datetime,
        reasons: list[str],
        *,
        run_id: int | None = None,
    ) -> RebaselineOutcome:
        with self._db.transaction() as tx:
            tx.execute(
                "UPDATE rebaseline_requests SET status = 'EXPIRED' "
                "WHERE id = ? AND status = 'PROPOSED'",
                (request_id,),
            )
            _event(tx, request_id, now, "EXPIRED", actor, reasons=reasons, run_id=run_id)
        return RebaselineOutcome(
            Decision.EXPIRED, request_id, tuple(reasons), reconciliation_run_id=run_id
        )


def cumulative_acknowledged(s: Session) -> Decimal:
    """Total unexplained cash acknowledged by approved rebaselines since inception."""
    rows = s.fetch_all(
        "SELECT acknowledged_amount FROM rebaseline_requests WHERE status = 'APPROVED'"
    )
    return sum((Decimal(str(r[0])) for r in rows), ZERO)


def _request(s: Session, request_id: int) -> dict[str, Any] | None:
    row = s.fetch_one(
        "SELECT id, created_at, status, diff_json, broker_snapshot_hash, unexplained_cash, "
        "acknowledged_amount FROM rebaseline_requests WHERE id = ?",
        (request_id,),
    )
    if row is None:
        return None
    keys = (
        "id",
        "created_at",
        "status",
        "diff_json",
        "broker_snapshot_hash",
        "unexplained_cash",
        "acknowledged_amount",
    )
    return dict(zip(keys, row, strict=True))


def _event(
    tx: Tx, request_id: int | None, at: datetime, action: str, actor: str, **detail: object
) -> None:
    tx.insert(
        "INSERT INTO rebaseline_events (request_id, ts, action, actor, detail_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (request_id, ts_to_db(at), action, actor, canonical_json(detail)),
    )


def _parse_amount(text: str) -> Decimal | None:
    if not text.strip():
        return None
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _require_text(**fields: str) -> None:
    for name, value in fields.items():
        if not value.strip():
            raise ValueError(f"an explicit {name} is required")


__all__ = [
    "ACKNOWLEDGEABLE",
    "ECONOMIC_TABLES",
    "PREVIEW_MAX_AGE",
    "AckItem",
    "Decision",
    "Preview",
    "RebaselineError",
    "RebaselineOutcome",
    "Rebaseliner",
    "cumulative_acknowledged",
]
