"""
Guided one-command setup for RakshaQuant.

Run this first:  uv run python scripts/setup.py

It creates your .env from the template if needed, runs the readiness check
(``scripts/check_config.py``) and prints the exact next command. Every key is optional: the
demo needs none. ASCII-only output so it works on any terminal.
"""

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

ENV = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
LINE = "=" * 64


def main() -> int:
    print(LINE)
    print(" RakshaQuant - Setup")
    print(LINE)
    print(
        f"[i] Python {sys.version_info.major}.{sys.version_info.minor} "
        f"({'OK' if sys.version_info >= (3, 11) else 'needs 3.11+'})"
    )

    # 1. Ensure a .env exists.
    if not ENV.exists():
        if ENV_EXAMPLE.exists():
            shutil.copy(ENV_EXAMPLE, ENV)
            print("[+] Created .env from .env.example")
        else:
            print("[!] .env.example not found - cannot create .env")
        print()
        print("[ACTION] Every key in .env is optional. Set only what you use:")
        print("   - LLM_ROLE_* and the key of each provider a role names (book C, reviews)")
        print("   - TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID for alerts")
        print()
        print("Then re-run:  uv run python scripts/setup.py")
        print(LINE)
        return 0

    print("[+] .env present")
    print()

    # 2. The readiness check (the same one scripts/check_config.py prints).
    sys.path.insert(0, str(ROOT / "scripts"))
    from check_config import main as check_config

    code = check_config()
    print()
    if code == 0:
        print("[NEXT] Synthetic demo, no keys needed:")
        print("         uv run python scripts/run_live_trading.py --demo")
        print("       Paper session in the web console:")
        print("         uv run python scripts/run_live_trading.py --mode web")
    print(LINE)
    return code


if __name__ == "__main__":
    from src.ops.process import run_entry_point

    run_entry_point("setup", main)
