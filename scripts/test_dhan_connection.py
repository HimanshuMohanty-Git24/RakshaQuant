"""
DhanHQ Connection Test

Tests the connection to DhanHQ API and fetches account info.
This uses PAPER TRADING mode only - no real money involved.

IMPORTANT: For sandbox tokens (from developer.dhan.co), use sandbox URL.
           For production tokens (from web.dhan.co), use api.dhan.co.
"""

import sys
from pathlib import Path

import requests

# Fix Windows encoding
sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import get_settings


def test_dhan_connection():
    """Test DhanHQ API connection using direct HTTP (supports sandbox)."""

    print("=" * 60)
    print("[SHIELD] RakshaQuant - DhanHQ Connection Test")
    print("=" * 60)

    settings = get_settings()

    # For sandbox tokens, use sandbox URL
    # For production tokens, use api.dhan.co
    base_url = settings.dhan_base_url

    print("\n[CONFIG] Configuration:")
    print(f"   Trading Mode: {settings.trading_mode}")
    print(f"   Client ID: {settings.dhan_client_id}")
    print(f"   API Base: {base_url}")

    headers = {
        "access-token": settings.dhan_access_token.get_secret_value(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    print("\n[CONNECT] Testing DhanHQ API...")

    try:
        # Test 1: Get fund limits
        print("\n[FUNDS] Fetching Fund Limits...")
        funds_url = f"{base_url}/fundlimit"
        funds_resp = requests.get(funds_url, headers=headers, timeout=10)
        funds = funds_resp.json()

        if funds.get("status") == "success":
            data = funds.get("data", {})
            print(f"   [OK] Available Balance: Rs.{data.get('availabelBalance', 'N/A')}")
            print(f"   [OK] Utilized Amount: Rs.{data.get('utilizedAmount', 'N/A')}")
        else:
            print(f"   [WARN] Response: {funds.get('remarks', funds)}")

        # Test 2: Get positions
        print("\n[POSITIONS] Fetching Positions...")
        pos_url = f"{base_url}/positions"
        pos_resp = requests.get(pos_url, headers=headers, timeout=10)
        positions = pos_resp.json()

        # API returns list directly or dict with status
        if isinstance(positions, list):
            print(f"   [OK] Open Positions: {len(positions)}")
        elif positions.get("status") == "success":
            pos_data = positions.get("data", [])
            print(f"   [OK] Open Positions: {len(pos_data)}")
        else:
            print(f"   [OK] No positions (or: {positions.get('remarks', 'N/A')})")

        # Test 3: Get order book
        print("\n[ORDERS] Fetching Order Book...")
        orders_url = f"{base_url}/orders"
        orders_resp = requests.get(orders_url, headers=headers, timeout=10)
        orders = orders_resp.json()

        # API returns list directly or dict with status
        if isinstance(orders, list):
            print(f"   [OK] Orders Today: {len(orders)}")
        elif orders.get("status") == "success":
            order_data = orders.get("data", [])
            print(f"   [OK] Orders Today: {len(order_data)}")
        else:
            print(f"   [OK] No orders (or: {orders.get('remarks', 'N/A')})")

        print("\n" + "=" * 60)
        print("[SUCCESS] DhanHQ Connection Test Complete!")
        print("=" * 60)

        return True

    except requests.exceptions.RequestException as e:
        print(f"\n[ERROR] Connection Error: {e}")
        return False
    except Exception as e:
        print(f"\n[ERROR] Error: {e}")
        return False


if __name__ == "__main__":
    print("\n[INFO] If using SANDBOX token (from developer.dhan.co):")
    print("       Set DHAN_BASE_URL=https://sandbox.dhan.co/v2 in .env")
    print("\n[INFO] If using PRODUCTION token (from web.dhan.co):")
    print("       Set DHAN_BASE_URL=https://api.dhan.co/v2 in .env")
    print()

    test_dhan_connection()
