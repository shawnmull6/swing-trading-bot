"""Order management: submission, lifecycle, synchronization and recovery (M1 step 12).

Runs only against :class:`~swing_trading_bot.broker.sim.SimBroker` in M1. Nothing in
this package holds a database transaction across a broker call.
"""
