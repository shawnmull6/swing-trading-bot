"""The historical acquisition runner (Step 16.17A; RESEARCH_DATA §3.0a-§3.0b, RUNBOOK §16).

The only package that connects the read-only Alpaca transport to the offline research
staging framework (:mod:`swing_trading_bot.research.staging`). It is kept outside
``research`` so that package stays free of any transport, credential or environment
access (``tests/unit/test_research_isolation.py``).

* :mod:`.transport` — the guarded HTTP session (GET only, the plan's exact endpoints and
  query keys, no redirects, no environment proxies or ``.netrc``, a hard attempt cap,
  timeouts cut to the session deadline) and the ``fetch`` adapter that turns one planned
  page into canonical, Decimal-safe bytes after validating its shape.
* :mod:`.runner` — authorization checks, gates from the reviewed probe evidence, the
  write-once session journal, explicit recovery, publication and status. It never opens
  a ledger, imports no OMS, execution or trading-enable module, runs no backtest and
  never opens the sealed holdout.

Live use is an authorized operator action only (``python -m
swing_trading_bot.history_acquisition``; RUNBOOK §16).
"""
