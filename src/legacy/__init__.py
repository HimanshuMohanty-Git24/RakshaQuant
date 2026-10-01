"""
Retired from the live path (plan M5.5); deleted in M12.

* ``agents/`` - the LangGraph pipeline (news, sentiment, prediction, LLM regime, LLM strategy
  selection, LLM signal validation, the graph risk node). The live decisions are now the
  deterministic :mod:`src.decision` engine and the :mod:`src.risk` RiskEngine.
* ``session.py`` - the old ``run_trading_session`` loop, replaced by :mod:`src.engine`.

Kept only so the legacy tests keep documenting the old behaviour until M12. Nothing outside
``src/legacy`` may import it (``tests/test_legacy_retirement.py`` enforces this).
"""
