"""Diagnose risk check failures."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime

from src.agents.risk_compliance import RiskLimits, _run_risk_checks

# Simulate a signal like what the system generates
signal = {
    "symbol": "ASIANPAINT",
    "signal_type": "BUY",
    "entry_price": 2775,
    "stop_loss": 2700,
    "target_price": 2900,
    "risk_reward_ratio": 1.66,
    "position_size_pct": 5.0,
    "confidence": 0.8,
    "validation": {"confidence": 0.7},
}

portfolio = {"capital": 1000000, "positions": []}
daily_stats = {"trades_count": 0, "profit_loss": 0, "max_drawdown": 0}
limits = RiskLimits.from_settings()

print("=" * 60)
print("RISK CHECK DIAGNOSIS")
print("=" * 60)
print(f"Current time: {datetime.now().strftime('%H:%M')}")
print(f"Trading hours: {limits.no_trading_before} - {limits.no_trading_after}")
print()

checks = _run_risk_checks(signal, portfolio, daily_stats, limits)

blocking = []
warnings = []

for check in checks:
    status = "PASS" if check.passed else "FAIL"
    if not check.passed:
        if check.severity == "block":
            blocking.append(check)
        else:
            warnings.append(check)
    print(f"[{status}] {check.rule}: {'OK' if check.passed else check.message}")

print()
print("=" * 60)
if blocking:
    print(f"BLOCKING FAILURES: {len(blocking)}")
    for b in blocking:
        print(f"  - {b.rule}: {b.message}")
else:
    print("NO BLOCKING FAILURES - Trade should be approved!")

if warnings:
    print(f"WARNINGS: {len(warnings)}")
print("=" * 60)
