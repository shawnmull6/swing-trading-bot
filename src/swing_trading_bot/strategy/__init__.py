"""Offline strategy research (Step 16.14, M2-4; ARCHITECTURE §12.45).

Pure, deterministic code with no I/O: a historical data contract
(:mod:`.market_data`), a support/resistance setup detector (:mod:`.support_resistance`),
the signal contract that translates into
:class:`~swing_trading_bot.domain.trade_plan.TradePlanIntent` (:mod:`.signals`) and an
offline historical replay (:mod:`.backtest`).

Nothing in this package submits orders, opens a ledger, reads credentials or talks to a
broker; ``tests/unit/test_strategy_isolation.py`` enforces that by import scanning. The
OMS knows nothing about how a setup was found: a strategy is replaceable as long as it
produces a valid intent.
"""
