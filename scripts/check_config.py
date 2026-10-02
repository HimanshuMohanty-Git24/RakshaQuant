"""Readiness check: environment and state directory, enabled LLM roles (exit 2 if one is
misconfigured), Dhan and Telegram status, and configuration warnings. Never prints a secret."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from src.config import get_settings
from src.config.errors import ConfigError
from src.llm.registry import validate_roles
from src.ops.exit_codes import ExitCode
from src.ops.process import run_entry_point


def main() -> int:
    """Print a readiness check; exit 2 if an enabled LLM role is misconfigured."""
    s = get_settings()

    print("=" * 50)
    print("RakshaQuant - Configuration Check")
    print("=" * 50)

    # LLM roles (plan M6): optional; an enabled role must name a known provider with its key.
    llm_ok = True
    try:
        roles = validate_roles(s)
        enabled = [r for r in roles.values() if r.enabled]
        if not enabled:
            print("LLM roles:        [None enabled - optional]")
        for role in enabled:
            print(f"LLM role {role.role:<8} {' -> '.join(m.spec for m in role.chain)}")
    except ConfigError as exc:
        llm_ok = False
        print(f"LLM roles:        [ERROR] {exc}")

    # Optional - DhanHQ
    dhan_ok = bool(s.dhan_client_id and s.dhan_access_token)
    print(f"DhanHQ API:       {'[OK]' if dhan_ok else '[Not configured - optional]'}")

    # Runtime
    print(f"\nEnvironment:      {s.environment}  (state: {s.state_dir})")
    print(f"Execution Mode:   {s.execution_mode}  (v2 trades on the simulated broker only)")

    # Telegram
    telegram_ok = bool(
        getattr(s, "telegram_bot_token", None) and getattr(s, "telegram_chat_id", None)
    )
    print(f"Telegram Alerts:  {'[OK]' if telegram_ok else '[Not configured - optional]'}")

    # Cross-field validation warnings (an ignored broker venue, Telegram token/chat pairing)
    config_warnings = getattr(s, "config_warnings", [])
    if config_warnings:
        print(f"\nConfiguration Warnings ({len(config_warnings)}):")
        for warning in config_warnings:
            print(f"  [WARN] {warning}")
    else:
        print("\nConfiguration Warnings:  [None]")

    print("\n" + "=" * 50)
    if llm_ok:
        if config_warnings:
            print(f"[READY] System ready to run ({len(config_warnings)} config warning(s) above).")
        else:
            print("[READY] System ready to run!")
    else:
        print("[ERROR] Fix the LLM role configuration above in .env")
    print("=" * 50)
    return ExitCode.OK if llm_ok else ExitCode.CONFIG_ERROR


if __name__ == "__main__":
    run_entry_point("check_config", main)
