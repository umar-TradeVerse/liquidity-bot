"""Multi-agent decision layer.

Flow for every confirmed setup:

    Structure Agent  (core/strategy.py — StrategyEngine)   proposes a Signal
          -> Context Agent  (context_agent.py)               approve / veto
          -> Risk Agent     (risk_agent.py)                  approve / veto / size
          -> Execution Agent (core/monitor.py + exchange/)   places the order
          -> Trade Manager   (core/monitor.py _check_*)      manages the position

Every setup produces exactly ONE decision record line in the logs
(see decision.py), showing which agent approved or vetoed it and why.
"""
