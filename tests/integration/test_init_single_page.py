"""Step 15B.8: ``init-ledger`` refuses unless its activity window was one page request.

Live multi-page cursor semantics are unverified (contract C7, A§12.31), so the 7-day
initialization window must be read in exactly one ``get_activities`` request: a full
first page (100 activities) always forces a confirming second request and refuses.
Offline only: the real read adapter over a fake HTTP account, temporary directories.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from swing_trading_bot.app import init_ledger
from swing_trading_bot.app.init_ledger import INIT_ACTIVITY_PAGE_SIZE, single_page_problems
from swing_trading_bot.broker.activity_feed import ActivityWindow
from swing_trading_bot.broker.errors import ServiceUnavailableError
from swing_trading_bot.ledger.repositories import SyncStateRepository
from swing_trading_bot.ledger.sync_keys import ACTIVITIES_COMPLETE_THROUGH
from tests.support.cli_env import CliEnv, cli_env

T = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)
ACTIVITIES = "/account/activities"


@pytest.fixture
def e(tmp_path: Path) -> CliEnv:
    return cli_env(tmp_path)


def post(e: CliEnv, n: int) -> None:
    for _ in range(n):
        e.account.deposit("1.00")


def confirm(e: CliEnv) -> tuple[str, ...]:
    return ("--confirm-path", str(e.ledger), "--confirm-account", e.account.account_id,
            "--confirm-environment", "paper")  # fmt: skip


def activity_requests(e: CliEnv) -> list[dict[str, str]]:
    return [params for _svc, path, params in e.account.transport.calls if path == ACTIVITIES]


def broker_state(e: CliEnv) -> tuple[Any, ...]:
    a = e.account
    return a.cash, list(a.positions), list(a.open_orders), list(a.activities)


def assert_nothing_created(e: CliEnv) -> None:
    assert not e.ledger.exists()
    assert sorted(p.name for p in e.data.iterdir()) == ["ledger.sqlite3.lock"]  # no sidecars


# ============================================================ accepted: one request
@pytest.mark.parametrize("n", [0, 1, 99])
def test_window_read_in_one_request_initializes(e: CliEnv, n: int) -> None:
    post(e, n)
    code, env = e.run("init-ledger")  # preview
    assert code == 3 and env["result"]["status"] == "CONFIRMATION_REQUIRED"
    evidence = env["result"]["evidence"]
    assert evidence["activity_pages"] == 1 and evidence["activities_in_lookback"] == n
    assert len(activity_requests(e)) == 1  # the metadata matches the real requests
    assert all("page_token" not in p for p in activity_requests(e))  # no cursor used
    assert all(p["page_size"] == str(INIT_ACTIVITY_PAGE_SIZE) for p in activity_requests(e))
    created = e.init()["result"]["created"]
    assert created["halted"] is True and created["halt_reason"] == "INITIALIZED"
    assert len(activity_requests(e)) == 2  # one more for the confirmed invocation
    with e.open() as db, db.read() as s:
        assert SyncStateRepository().get(s, ACTIVITIES_COMPLETE_THROUGH) is None  # unchanged


# ============================================================ refused: more than one request
@pytest.mark.parametrize(("n", "pages"), [(100, 2), (150, 2), (200, 3), (250, 3)])
def test_window_needing_more_than_one_request_refuses(e: CliEnv, n: int, pages: int) -> None:
    """Exactly 100 is a full first page: the confirming (empty) second request refuses."""
    post(e, n)
    before = broker_state(e)
    code, env = e.run("init-ledger", *confirm(e))
    assert code == 3 and env["result"]["status"] == "REFUSED"
    assert env["result"]["evidence"]["activity_pages"] == pages
    assert any(f"needed {pages} page requests" in p for p in env["result"]["problems"])
    requests = activity_requests(e)
    assert len(requests) == pages and "page_token" in requests[-1]  # really paginated
    assert_nothing_created(e)
    assert broker_state(e) == before  # GET only: the broker side is untouched
    assert env["result"]["created"] is None


def test_refusal_repeats_and_never_creates_anything(e: CliEnv) -> None:
    post(e, 100)
    for argv in [(), confirm(e), confirm(e)]:
        code, env = e.run("init-ledger", *argv)
        assert code == 3 and env["result"]["status"] == "REFUSED"
        assert_nothing_created(e)


def test_incomplete_retrieval_creates_nothing(e: CliEnv) -> None:
    e.account.fail(ACTIVITIES, *[ServiceUnavailableError("503")] * 4)
    code, env = e.run("init-ledger", *confirm(e))
    assert code == 8 and env["result"]["status"] == "BROKER_UNAVAILABLE"  # existing class
    assert_nothing_created(e)


# ============================================================ metadata the collector cannot
# produce (it always returns a confirmed tail and a positive page count): injected windows.
def window(pages: object, n: int = 0, *, tail: bool = True) -> ActivityWindow:
    acts = tuple(
        SimpleNamespace(activity_id=f"a{i:04d}", activity_type="CSD", raw_json="{}")
        for i in range(n)
    )
    return ActivityWindow(None, T, acts, pages, tail_confirmed=tail, closed=False)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("w", "problem"),
    [
        (window(None), "missing or invalid"),
        (window(0), "missing or invalid"),
        (window(-1), "missing or invalid"),
        (window(True), "missing or invalid"),  # bool is not a page count
        (window("1"), "missing or invalid"),
        (window(Decimal(1)), "missing or invalid"),
        (window(1, INIT_ACTIVITY_PAGE_SIZE), "inconsistent with a full page"),
        (window(1, INIT_ACTIVITY_PAGE_SIZE + 1), "inconsistent with a full page"),
        (window(2, 100), "needed 2 page requests"),
    ],
)
def test_page_metadata_rules(w: ActivityWindow, problem: str) -> None:
    (got,) = single_page_problems(w)
    assert problem in got


@pytest.mark.parametrize("n", [0, 1, INIT_ACTIVITY_PAGE_SIZE - 1])
def test_single_short_page_has_no_problem(n: int) -> None:
    assert single_page_problems(window(1, n)) == []


@pytest.mark.parametrize(
    ("w", "problem"),
    [
        (window(None), "page count is missing or invalid"),
        (window(1, INIT_ACTIVITY_PAGE_SIZE), "inconsistent with a full page"),
        (window(1, tail=False), "could not be read to its end"),
    ],
)
def test_injected_bad_window_refuses_end_to_end(
    e: CliEnv, monkeypatch: pytest.MonkeyPatch, w: ActivityWindow, problem: str
) -> None:
    monkeypatch.setattr(init_ledger, "collect_window", lambda *_a, **_k: w)
    before = broker_state(e)
    code, env = e.run("init-ledger", *confirm(e))
    assert code == 3 and env["result"]["status"] == "REFUSED"
    assert any(problem in p for p in env["result"]["problems"]), env["result"]["problems"]
    assert_nothing_created(e)
    assert broker_state(e) == before
