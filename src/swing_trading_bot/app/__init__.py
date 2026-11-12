"""Runtime orchestration and the operator CLI (M1 step 14, ARCHITECTURE §12.20).

Observe-mode modules here never construct an execution capability; the only module that
does is :mod:`swing_trading_bot.app.simulation`, which works exclusively on new
simulation ledgers with an in-memory SimBroker.
"""
