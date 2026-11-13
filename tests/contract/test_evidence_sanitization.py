"""Step 15B.5C: structured sanitization of live contract evidence — OFFLINE, synthetic
secrets and identifiers only.

Live run #3 showed the old capture-time redactor (the log redactor's key=value regex over
the raw text) turning ``"next_page_token": null`` into invalid JSON, and
``forbidden_values`` silently skipping bodies it could not parse — so identifiers from
those bodies were never checked against the summary. Now the original body stays in
memory, is parsed into a structure, redacted by an explicit policy, serialized
canonically and verified before anything is written; any doubt fails closed as
``SUMMARY_SANITIZATION_FAILED``.
"""

from __future__ import annotations

import io
import json
import logging
import stat
import subprocess
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import requests
from hypothesis import given
from hypothesis import strategies as st

from swing_trading_bot.observability.logging import SecretRedactor
from tests.contract import checks as contract_checks
from tests.contract.live_run import fail_if_not_ok, render, run_supervised
from tests.contract.supervisor import (
    MAX_EVIDENCE_DEPTH,
    REDACTED,
    BudgetedSession,
    EvidenceWriter,
    Exchange,
    RequestBudget,
    SanitizationError,
    canonical_json,
    forbidden_values,
    identity_values,
    sanitize_body,
)
from tests.contract.test_supervised_run import ENV, KEY, SECRET, FakeTime, HarnessSession
from tests.support.alpaca_account import ACCOUNT_ID

D = Decimal
ACCOUNT_NUMBER = "PA0000"  # synthetic (tests/support/alpaca_fake.py)
IDENTITIES = (KEY, SECRET, ACCOUNT_ID, ACCOUNT_NUMBER)


def exchange(body: object, *, status: int = 200, path: str = "/v2/x") -> Exchange:
    text = body if isinstance(body, str) else canonical_json(body)
    return Exchange("GET", "paper-api.alpaca.markets", path, status, {}, text)


def account_exchange() -> Exchange:
    return exchange({"id": ACCOUNT_ID, "account_number": ACCOUNT_NUMBER, "cash": "100000.00"},
                    path="/v2/account")  # fmt: skip


def persisted(*items: Exchange, secrets: tuple[str, ...] = (KEY, SECRET)) -> list[dict[str, Any]]:
    ids = identity_values(items, secrets)
    return [json.loads(sanitize_body(ex, ids)) for ex in items]


def body_of(record: dict[str, Any]) -> Any:
    return json.loads(record["body"], parse_float=Decimal)


# ============================================================ the original defect
def test_the_old_regex_redactor_corrupted_unquoted_json_values() -> None:
    """Documents the live run #3 defect (the log redactor is unchanged and still used for
    logs; it is no longer applied to evidence bodies)."""
    text = json.dumps({"bars": {}, "next_page_token": None})
    with pytest.raises(json.JSONDecodeError):
        json.loads(SecretRedactor().redact(text))


def test_next_page_token_null_stays_valid_json() -> None:
    (rec,) = persisted(exchange({"bars": {"SPY": []}, "next_page_token": None}))
    assert '"next_page_token":null' in rec["body"]
    assert body_of(rec) == {"bars": {"SPY": []}, "next_page_token": None}


# ============================================================ type preservation
DOC: dict[str, Any] = {
    "null": None, "t": True, "f": False, "int": 7, "big": 123456789012345678901234567890,
    "neg": -3, "dec": D("1.50"), "exp": D("2.5E+3"), "tiny": D("0.000000001"),
    "arr": [1, "two", None, [True, {"deep": D("0.10")}]], "obj": {"a": {"b": {"c": []}}},
    "money_str": "100.10", "empty": "", "unicode": "ünï",
}  # fmt: skip


def raw_text(doc: object) -> str:
    """JSON text with exact decimals (as a broker would send them)."""
    return canonical_json(doc)


def test_every_json_type_is_preserved_exactly() -> None:
    (rec,) = persisted(exchange(raw_text(DOC)))
    assert body_of(rec) == DOC
    assert '"dec":1.50' in rec["body"] and '"tiny":1E-9' in rec["body"]  # Decimal, not float
    assert '"null":null' in rec["body"] and '"t":true' in rec["body"]


def test_output_is_deterministic_and_canonical() -> None:
    a = persisted(exchange('{"b": 1, "a": [1.50, null]}'))[0]["body"]
    b = persisted(exchange('{"a":[1.50,null],"b":1}'))[0]["body"]
    assert a == b == '{"a":[1.50,null],"b":1}'


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text()
    | st.decimals(allow_nan=False, allow_infinity=False),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(), inner, max_size=4),
    max_leaves=25,
)  # fmt: skip


@given(json_values, st.sampled_from([KEY, SECRET, ACCOUNT_ID]))
def test_any_document_with_an_embedded_secret_stays_valid_and_leak_free(
    doc: Any, secret: str
) -> None:
    hidden = {"wrapper": doc, "note": f"x{secret}y", secret: [secret, {"k": secret}]}
    ids = identity_values([account_exchange()], (KEY, SECRET))
    text = sanitize_body(exchange(raw_text(hidden)), ids)
    record = json.loads(text)
    json.loads(record["body"], parse_float=Decimal)  # still valid JSON
    assert all(v not in text for v in IDENTITIES)


@given(st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(), inner, max_size=4),
    max_leaves=25,
))  # fmt: skip
def test_canonical_json_matches_sorted_compact_json_dumps(doc: Any) -> None:
    assert canonical_json(doc) == json.dumps(doc, sort_keys=True, separators=(",", ":"))


# ============================================================ redaction policy
def test_credential_keys_hide_strings_and_numbers_but_keep_null_and_booleans() -> None:
    doc = {
        "api_key": "abc123", "APCA-API-SECRET-KEY": "s", "next_page_token": "cursor-1",
        "page_token": None, "token_valid": True, "password": 1234, "secret_ratio": D("0.5"),
        "auth": {"credentials": {"user": "u", "nested": ["a", None, 5]}},
        "authorization": ["Bearer x", "Bearer y"], "symbol": "SPY",
    }  # fmt: skip
    got = body_of(persisted(exchange(doc))[0])
    assert got == {
        "api_key": REDACTED, "APCA-API-SECRET-KEY": REDACTED, "next_page_token": REDACTED,
        "page_token": None, "token_valid": True, "password": REDACTED,
        "secret_ratio": REDACTED,
        "auth": {"credentials": {"user": REDACTED, "nested": [REDACTED, None, REDACTED]}},
        "authorization": [REDACTED, REDACTED], "symbol": "SPY",
    }  # fmt: skip


def test_account_identifiers_are_hidden_at_any_depth_and_everywhere_else() -> None:
    other = exchange({"activities": [{"meta": {"account_id": "acct-9999", "account_number":
                     ACCOUNT_NUMBER}}, {"description": f"transfer to {ACCOUNT_ID}"}]})  # fmt: skip
    acct, rec = persisted(account_exchange(), other)
    assert body_of(acct) == {"id": REDACTED, "account_number": REDACTED, "cash": "100000.00"}
    assert body_of(rec) == {"activities": [
        {"meta": {"account_id": REDACTED, "account_number": REDACTED}},
        {"description": f"transfer to {REDACTED}"}]}  # fmt: skip
    assert "acct-9999" in identity_values([other], ())  # nested identifiers are collected


def test_secrets_in_values_keys_and_escaped_form_are_removed() -> None:
    escaped = "".join(f"\\u{ord(c):04x}" for c in KEY)  # JSON-escaped: a regex never saw it
    body = f'{{"msg": "key {escaped}", "{SECRET}": "v", "list": ["{SECRET}"]}}'
    (rec,) = persisted(exchange(body))
    text = json.dumps(rec)
    assert KEY not in text and SECRET not in text and escaped not in text
    # the original key contains "Secret": a credential-looking key hides its value too
    assert body_of(rec) == {"msg": f"key {REDACTED}", REDACTED: REDACTED, "list": [REDACTED]}


def test_non_json_text_gets_exact_replacement_only() -> None:
    text = f"<html>token=abc null true 1.50 key {KEY} acct {ACCOUNT_ID}</html>"
    _, rec = persisted(account_exchange(), exchange(text, status=502))
    assert rec["status"] == 502
    assert rec["body"] == f"<html>token=abc null true 1.50 key {REDACTED} acct {REDACTED}</html>"


def test_error_json_bodies_are_structurally_sanitized() -> None:
    error = exchange({"code": 40010001, "message": f"bad {SECRET}"}, status=403)
    _, rec = persisted(account_exchange(), error)
    assert body_of(rec) == {"code": 40010001, "message": f"bad {REDACTED}"}


# ============================================================ fail closed
@pytest.mark.parametrize("body", ['{"a": ', "<html>oops</html>", '{"x": NaN}', '[Infinity]',
                                  "[" * 100_000])  # fmt: skip
def test_malformed_expected_json_is_a_sanitization_failure(body: str) -> None:
    ex = exchange(f"{body} {SECRET}")
    with pytest.raises(SanitizationError) as info:
        forbidden_values([ex], (SECRET,))
    assert info.value.code == "EVIDENCE_JSON_MALFORMED"
    with pytest.raises(SanitizationError):
        sanitize_body(ex, (SECRET,))
    assert str(info.value) == "EVIDENCE_JSON_MALFORMED"  # no content, value or index
    assert info.value.__cause__ is None and info.value.__suppress_context__


@pytest.mark.parametrize("status", [200, 500])
def test_excessive_nesting_fails_closed_without_recursion_errors(status: int) -> None:
    deep = "[" * (MAX_EVIDENCE_DEPTH + 1) + "]" * (MAX_EVIDENCE_DEPTH + 1)
    for fn in (lambda e: forbidden_values([e], ()), lambda e: sanitize_body(e, ())):
        with pytest.raises(SanitizationError, match="EVIDENCE_TOO_DEEP"):
            fn(exchange(deep, status=status))
    ok = "[" * MAX_EVIDENCE_DEPTH + "]" * MAX_EVIDENCE_DEPTH
    assert json.loads(sanitize_body(exchange(ok), ()))["body"] == ok


def test_old_behavior_skipped_malformed_bodies_now_nothing_is_skipped() -> None:
    nested = exchange({"data": {"positions": [{"symbol": "ZZZZ", "market_value": D("12345.6"),
                                               "qty": 10}]}})  # fmt: skip
    found = forbidden_values([nested], ())
    assert {"ZZZZ", "12345.6"} <= set(found)  # nested and numeric sensitive values
    assert "10" not in found  # short values are not collected (unchanged rule)


@pytest.mark.parametrize("bad", [object(), {1, 2}, b"x", 1.5, {1: "x"}, D("NaN"), D("-Infinity")])
def test_unsupported_types_are_never_coerced(bad: object) -> None:
    with pytest.raises(SanitizationError) as info:
        canonical_json({"x": bad})
    assert info.value.code in {"EVIDENCE_UNSUPPORTED_TYPE", "EVIDENCE_NON_FINITE_NUMBER"}


def test_an_identity_surviving_as_a_number_is_detected_after_sanitization() -> None:
    acct = exchange({"id": "a", "account_number": "88887777"}, path="/v2/account")
    other = exchange({"reference": 88887777})  # not a string: cannot be scrubbed
    ids = identity_values([acct, other], ())
    with pytest.raises(SanitizationError, match="EVIDENCE_FORBIDDEN_VALUE"):
        sanitize_body(other, ids)


def test_a_redaction_key_collision_fails_closed() -> None:
    with pytest.raises(SanitizationError, match="EVIDENCE_KEY_COLLISION"):
        persisted(exchange({SECRET: 1, REDACTED: 2}))


def test_writer_writes_nothing_when_any_body_fails(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path / "ev", run="r", require_git_ignored=False)
    with pytest.raises(SanitizationError):
        writer.write([account_exchange(), exchange("{bad")], {"ok": True}, [], secrets=(KEY,))
    assert not (tmp_path / "ev").exists()


def test_session_keeps_the_original_body_in_memory_only() -> None:
    clock = FakeTime()
    spy = BudgetedSession(HarnessSession(clock), RequestBudget(monotonic=clock))
    spy.get("https://paper-api.alpaca.markets/v2/account", allow_redirects=False,
            timeout=(5, 5), params={}, headers={})  # fmt: skip
    (ex,) = spy.exchanges
    assert ACCOUNT_ID in ex.body and ACCOUNT_ID not in repr(ex)


# ============================================================ supervised run (offline)
@pytest.fixture
def logs() -> Iterator[io.StringIO]:
    target = logging.getLogger("swing_trading_bot")
    saved = (list(target.handlers), target.propagate, target.level)
    stream = io.StringIO()
    yield stream
    target.handlers[:] = saved[0]
    target.propagate, target.level = saved[1], saved[2]


def run(tmp_path: Path, logs: io.StringIO, session: HarnessSession, clock: FakeTime
        ) -> dict[str, Any]:  # fmt: skip
    return run_supervised(
        ENV, budget=RequestBudget(monotonic=clock), session_factory=lambda: session,
        sleep=clock.sleep, now=lambda: session.account.clock.now,
        evidence=EvidenceWriter(tmp_path / "evidence", run="run", require_git_ignored=False),
        log_stream=logs, log_target=logging.getLogger("swing_trading_bot"),
    )  # fmt: skip


def evidence_files(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "evidence").rglob("*.json"))


def test_completed_run_persists_valid_leak_free_evidence(tmp_path: Path, logs: io.StringIO
                                                         ) -> None:  # fmt: skip
    clock = FakeTime()
    summary = run(tmp_path, logs, HarnessSession(clock), clock)
    assert summary["execution"]["status"] == "COMPLETED"
    assert summary["rows"]["C1"]["outcome"] == "VERIFIED"  # outcomes unchanged
    raw = [p for p in evidence_files(tmp_path) if p.parent.name == "raw"]
    assert len(raw) > 5
    bars = []
    for path in raw:
        text = path.read_text()
        assert all(v not in text for v in IDENTITIES), path.name
        record = json.loads(text)
        assert set(record) == {"status", "body"}  # unchanged evidence file layout
        if 200 <= record["status"] < 300:
            body = json.loads(record["body"], parse_float=Decimal)  # valid JSON again
            if "bars" in path.name:
                bars.append(body)
    assert bars and all("next_page_token" in b and b["next_page_token"] is None for b in bars)
    corpus = json.dumps(summary) + logs.getvalue() + render(summary)
    assert all(v not in corpus for v in (*IDENTITIES, "100000.00"))
    for d in (tmp_path / "evidence", tmp_path / "evidence" / "run",
              tmp_path / "evidence" / "run" / "raw"):  # fmt: skip
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in evidence_files(tmp_path))


def test_echoed_error_body_is_scrubbed_in_raw_evidence(tmp_path: Path, logs: io.StringIO
                                                        ) -> None:  # fmt: skip
    clock = FakeTime()
    session = HarnessSession(clock)
    session.echo_paths = {"/stocks/trades/latest"}  # a 500 body echoing every identity
    summary = run(tmp_path, logs, session, clock)
    assert summary["rows"]["C8"] == {"outcome": "INCONCLUSIVE", "code": "BROKER_UNAVAILABLE_HTTP"}
    echoes = [p for p in evidence_files(tmp_path) if "trades_latest" in p.name]
    assert echoes  # one file per attempt (the adapter retries a 500)
    for echo in echoes:
        record = json.loads(echo.read_text())
        assert record["status"] == 500
        assert body_of(record) == {"message": f"key {REDACTED} secret {REDACTED} acct {REDACTED}"}
    for path in evidence_files(tmp_path):
        assert all(v not in path.read_text() for v in IDENTITIES), path.name


class MalformedSession(HarnessSession):
    """A successful response whose body is not JSON (and contains a secret)."""

    def get(self, url: str, **kw: Any) -> Any:
        response = super().get(url, **kw)
        if url.endswith("/v2/clock"):
            r = requests.Response()
            r.url, r.status_code = url, 200
            r._content = f"<html>{SECRET} {ACCOUNT_ID}</html>".encode()
            r.headers.update(self.headers)
            return r
        return response


def test_malformed_evidence_fails_closed_and_nothing_is_verified(
    tmp_path: Path, logs: io.StringIO
) -> None:
    clock = FakeTime()
    summary = run(tmp_path, logs, MalformedSession(clock), clock)
    assert summary == {
        "execution": {"status": "STOPPED", "stop_code": "SUMMARY_SANITIZATION_FAILED"},
        "evidence": {"written": False, "code": "EVIDENCE_JSON_MALFORMED"},
    }
    assert "VERIFIED" not in json.dumps(summary) and "rows" not in summary
    assert not (tmp_path / "evidence").exists()  # nothing persisted, raw or summary
    corpus = render(summary) + logs.getvalue()
    assert all(v not in corpus for v in IDENTITIES) and "<html>" not in corpus
    with pytest.raises(pytest.fail.Exception):
        fail_if_not_ok(summary)


def test_forbidden_value_in_the_summary_fails_closed(
    tmp_path: Path, logs: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    def leaky(_c: Any) -> dict[str, Any]:
        return {"cash_seen": "100000.00"}  # a balance from the original account body

    monkeypatch.setitem(contract_checks.CHECKS, "clock", leaky)
    clock = FakeTime()
    summary = run(tmp_path, logs, HarnessSession(clock), clock)
    assert summary["execution"]["stop_code"] == "SUMMARY_SANITIZATION_FAILED"
    assert summary["evidence"] == {"written": False, "code": "SUMMARY_FORBIDDEN_VALUE"}
    assert "rows" not in summary and not (tmp_path / "evidence").exists()
    assert "100000.00" not in logs.getvalue() + render(summary)


def test_evidence_root_is_git_ignored_and_untracked() -> None:
    from tests.contract.supervisor import EVIDENCE_ROOT, REPO, _git_ignored  # noqa: PLC0415

    assert _git_ignored(EVIDENCE_ROOT / "run" / "raw" / "000-paper_v2_account.json")
    tracked = subprocess.run(
        ["git", "ls-files", "contract-evidence"],  # noqa: S607
        cwd=REPO, capture_output=True, text=True, timeout=5, check=True,
    )  # fmt: skip
    assert tracked.stdout == ""


def test_empty_and_non_json_error_bodies_contribute_no_structured_values() -> None:
    items = [exchange("", status=-1), exchange(f"<html>{SECRET}</html>", status=503),
             exchange({"id": {"nested": "obj"}, "symbol": None, "cash": True})]  # fmt: skip
    assert forbidden_values(items, (SECRET,)) == [SECRET]
    assert json.loads(sanitize_body(items[0], ())) == {"status": -1, "body": ""}


def test_an_unserializable_summary_fails_closed(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path / "ev", run="r", require_git_ignored=False)
    with pytest.raises(SanitizationError, match="SUMMARY_UNSERIALIZABLE"):
        writer.write([], {"x": object()}, [])
    assert not (tmp_path / "ev").exists()
