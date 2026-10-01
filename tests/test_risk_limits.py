"""Plan M4.5: bounded risk limits that raise at startup (audit F-15)."""

import pytest

from src.config.errors import ConfigError
from src.config.limits import RiskLimits, load_risk_limits
from src.ops.exit_codes import ExitCode
from src.ops.process import run


def test_defaults_are_the_documented_month1_limits():
    limits = load_risk_limits()
    assert (limits.risk_per_trade, limits.max_position_pct, limits.max_positions) == (0.02, 0.10, 5)
    assert limits.max_gross_exposure_pct == 0.50 and limits.max_drawdown_pct == 0.05
    assert limits.enabled_strategies == ("momentum", "mean_reversion")
    assert limits.allow_short is False and limits.kelly_enabled is False


def test_limits_hash_is_stable_and_sensitive():
    a, b = load_risk_limits(), load_risk_limits()
    assert a.limits_hash() == b.limits_hash() and len(a.limits_hash()) == 12
    assert load_risk_limits({"max_positions": 6}).limits_hash() != a.limits_hash()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("risk_per_trade", 0.5),  # the audit F-15 probe values
        ("max_position_pct", 5.0),
        ("daily_loss_limit_pct", 1e12),
        ("max_entries_per_day", 1_000_000_000),
        ("daily_loss_limit_pct", 0.0),  # <= 0 used to fire the kill switch every cycle
        ("max_positions", 0),
        ("max_positions", 21),
        ("max_drawdown_pct", -0.1),
        ("kelly_min_trades", 10),
        ("max_quote_age_s", 0),
    ],
)
def test_out_of_bounds_values_raise(field, value):
    with pytest.raises(ConfigError, match=field):
        load_risk_limits({field: value})


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"stop_atr_min": 3.0, "stop_atr_max": 2.0}, "stop_atr_min"),
        ({"risk_per_trade": 0.05, "max_position_pct": 0.04}, "risk_per_trade"),
        ({"max_position_pct": 0.25, "max_gross_exposure_pct": 0.2}, "max_position_pct"),
        ({"enabled_strategies": ()}, "enabled_strategies"),
    ],
)
def test_inconsistent_combinations_raise(values, message):
    with pytest.raises(ConfigError, match=message):
        load_risk_limits(values)


def test_limits_come_from_risk_env_vars(monkeypatch):
    monkeypatch.setenv("RISK_MAX_POSITIONS", "7")
    assert load_risk_limits().max_positions == 7
    monkeypatch.setenv("RISK_RISK_PER_TRADE", "0.5")
    with pytest.raises(ConfigError, match="risk_per_trade"):
        load_risk_limits()


def test_limits_are_frozen():
    with pytest.raises(Exception):
        RiskLimits().max_positions = 9  # type: ignore[misc]


def test_a_limits_violation_at_startup_exits_2(monkeypatch, capsys):
    monkeypatch.setenv("RISK_MAX_POSITIONS", "99")
    assert run("probe", lambda: (load_risk_limits(), 0)[1]) == ExitCode.CONFIG_ERROR
    assert "max_positions" in capsys.readouterr().err
