# swing-trading-bot

A paper-only, fail-closed swing-trading bot core (milestone **M1**: infrastructure and
portfolio accounting). It keeps a persistent **$5,000 virtual ledger**, reads a dedicated
Alpaca **paper** account strictly read-only, reconciles everything the broker reports, and
halts on anything it cannot explain. M1 places **no** orders at Alpaca; order execution
exists only against an in-memory simulator.

* Architecture and every as-built decision: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
* Plan, decisions and acceptance criteria: [`docs/M1_PLAN.md`](docs/M1_PLAN.md)
* Operating procedures: [`docs/RUNBOOK.md`](docs/RUNBOOK.md)
* Performance measurements: [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md)
* External references (pinned, read-only; never installed): [`docs/EXTERNAL_REFERENCES.md`](docs/EXTERNAL_REFERENCES.md)

## Quick start (offline)

```bash
uv sync
```
```bash
uv run swing-trading-bot simulate all --data-dir /tmp/stb
```

`simulate` runs scripted scenarios against the SimBroker on new simulation ledgers; its
output is labelled hypothetical.

## Talking to the paper account (read-only)

Select credentials explicitly — environment variables, or `STB_ENV_FILE=/absolute/path`
to a `chmod 600` file that is not tracked by Git. No `.env` is read implicitly.

```bash
STB_ENV_FILE=/abs/path/paper.env uv run swing-trading-bot broker-check
```

See the runbook for `init-ledger`, `sync`, `reconcile`, `halt`/`resume`, rebaseline,
backup/restore and migrations. (The former `scripts/check_connection.py` was removed; it
used the SDK trading client, which has write methods.)

## Development

```bash
uv run pytest
```
```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Tests are network-free. Live read-only contract tests (`RUN_ALPACA_PAPER_TESTS=1`) and
benchmarks (`RUN_PERF=1`) are opt-in; contract runs require explicit operator
authorization.
