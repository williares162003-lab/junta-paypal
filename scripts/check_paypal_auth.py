"""Check that the PayPal sandbox credentials in .env work. Never prints secrets.

Run: python scripts/check_paypal_auth.py
"""
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env(path=ROOT / ".env"):
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def main():
    env = load_env()
    base = env.get("PAYPAL_BASE_URL", "")
    if "sandbox" not in base:
        sys.exit("Refusing to run: PAYPAL_BASE_URL must point to the sandbox.")
    client_id, secret = env.get("PAYPAL_CLIENT_ID", ""), env.get("PAYPAL_CLIENT_SECRET", "")
    if not client_id or not secret:
        sys.exit("Missing PAYPAL_CLIENT_ID or PAYPAL_CLIENT_SECRET in .env")

    auth = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    request = urllib.request.Request(
        f"{base}/v1/oauth2/token",
        data=b"grant_type=client_credentials",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as err:
        body = err.read().decode(errors="replace")
        print(f"FAILED: HTTP {err.code}. PayPal says: {body[:300]}")
        sys.exit(1)

    scopes = data.get("scope", "").split()
    print("OK: credentials work. Token type:", data.get("token_type"), "| expires in:", data.get("expires_in"), "s")
    wanted = {
        "Invoicing": "https://uri.paypal.com/services/invoicing",
        "Payouts": "https://uri.paypal.com/payments/payouts",
        "Refunds": "https://uri.paypal.com/services/payments/refund",
    }
    for name, scope in wanted.items():
        print(f"  {name:10s} {'yes' if scope in scopes else 'NO'}")


if __name__ == "__main__":
    main()
