"""
Plan D2 / M0.2: the paper wallet restarts at Rs.10,00,000.

The pre-v2 state was archived to ``var/archive/2026-10-01/``; until the M3 engine lands, the
legacy ``LocalPaperEngine`` must also start a fresh wallet at Rs.10,00,000.
"""

from unittest.mock import patch

from src.execution.costs import CostModel
from src.execution.paper_engine import LocalPaperEngine

TEN_LAKH = 1_000_000.0


def test_default_paper_wallet_balance_is_ten_lakh(settings):
    assert settings.paper_wallet_balance == TEN_LAKH


def test_fresh_legacy_engine_starts_at_ten_lakh(tmp_path, settings):
    with patch("src.execution.paper_engine.get_settings", return_value=settings):
        engine = LocalPaperEngine(state_file=tmp_path / "wallet.json", cost_model=CostModel.zero())

    assert engine.get_balance() == TEN_LAKH
    assert engine.get_total_value() == TEN_LAKH
    assert engine.get_positions() == []
