"""PayPal sandbox viability test for Junta: contribution (order -> approval -> capture) and pot (payout).

Usage:
  python scripts/viability_test.py order 100.00           # create a contribution order, prints the approval link
  python scripts/viability_test.py capture <ORDER_ID>     # capture it after the member approved it
  python scripts/viability_test.py payout <EMAIL> 1.00    # send a payout from the organizer (business) account
  python scripts/viability_test.py payout-status <BATCH_ID>

Sandbox only. Every PayPal id and status is appended to data/viability_log.json. Secrets are never printed.
"""
import base64
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from check_paypal_auth import load_env  # noqa: E402

ENV = load_env()
BASE = ENV["PAYPAL_BASE_URL"]
if "sandbox" not in BASE:
    sys.exit("Refusing to run: PAYPAL_BASE_URL must point to the sandbox.")
LOG = ROOT / "data" / "viability_log.json"


def money(text):
    """Validate and normalize an amount to two decimals (as a string, never a float)."""
    value = Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if value <= 0 or value > Decimal("1000"):
        sys.exit("Amount must be between 0.01 and 1000.00 for this test.")
    return f"{value:.2f}"


def token():
    auth = base64.b64encode(f"{ENV['PAYPAL_CLIENT_ID']}:{ENV['PAYPAL_CLIENT_SECRET']}".encode()).decode()
    req = urllib.request.Request(f"{BASE}/v1/oauth2/token", data=b"grant_type=client_credentials",
                                 headers={"Authorization": f"Basic {auth}",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["access_token"]


def call(method, path, body=None, request_id=None):
    headers = {"Authorization": f"Bearer {token()}", "Content-Type": "application/json"}
    if request_id:
        headers["PayPal-Request-Id"] = request_id  # idempotency: a retry with the same id is not repeated
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def log(event):
    LOG.parent.mkdir(exist_ok=True)
    entries = json.loads(LOG.read_text(encoding="utf-8")) if LOG.exists() else []
    entries.append({"at": time.strftime("%Y-%m-%d %H:%M:%S"), **event})
    LOG.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def create_order(amount):
    amount = money(amount)
    request_id = str(uuid.uuid4())
    status, body = call("POST", "/v2/checkout/orders", {
        "intent": "CAPTURE",
        "purchase_units": [{"reference_id": "junta-test-contribution",
                            "description": "Junta test contribution (sandbox)",
                            "amount": {"currency_code": "USD", "value": amount}}],
        "application_context": {"brand_name": "Junta (sandbox test)", "user_action": "PAY_NOW",
                                "shipping_preference": "NO_SHIPPING"},
    }, request_id)
    approve = next((l["href"] for l in body.get("links", []) if l.get("rel") in ("approve", "payer-action")), None)
    log({"step": "create_order", "http": status, "order_id": body.get("id"), "status": body.get("status"),
         "amount": amount, "request_id": request_id})
    print(f"HTTP {status} | order {body.get('id')} | status {body.get('status')} | amount USD {amount}")
    if approve:
        print("Approval link (open it and log in with a PERSONAL sandbox account):")
        print(approve)
    else:
        print(json.dumps(body, indent=2)[:800])


def capture(order_id):
    status, body = call("GET", f"/v2/checkout/orders/{order_id}")
    print(f"Order {order_id} is {body.get('status')}")
    if body.get("status") != "APPROVED":
        log({"step": "capture_skipped", "order_id": order_id, "status": body.get("status")})
        print("Not approved yet: the member must approve it first.")
        return
    status, body = call("POST", f"/v2/checkout/orders/{order_id}/capture", None, f"capture-{order_id}")
    cap = (body.get("purchase_units") or [{}])[0].get("payments", {}).get("captures", [{}])[0]
    payer = body.get("payer", {}).get("email_address")
    log({"step": "capture", "http": status, "order_id": order_id, "order_status": body.get("status"),
         "capture_id": cap.get("id"), "capture_status": cap.get("status"),
         "amount": cap.get("amount", {}).get("value"), "payer": payer})
    print(f"HTTP {status} | order {body.get('status')} | capture {cap.get('id')} {cap.get('status')} "
          f"| USD {cap.get('amount', {}).get('value')} | payer {payer}")
    if status >= 400:
        print(json.dumps(body, indent=2)[:800])


def payout(email, amount):
    amount = money(amount)
    allowed = {e.strip() for e in ENV.get("MEMBER_EMAILS", "").split(",") if e.strip()}
    if email not in allowed:
        sys.exit("Recipient must be one of the sandbox MEMBER_EMAILS in .env")
    batch_id = f"junta-test-{uuid.uuid4().hex[:12]}"
    status, body = call("POST", "/v1/payments/payouts", {
        "sender_batch_header": {"sender_batch_id": batch_id, "email_subject": "Junta: your pot (sandbox test)"},
        "items": [{"recipient_type": "EMAIL", "receiver": email, "note": "Junta pot (sandbox test)",
                   "sender_item_id": f"{batch_id}-1", "amount": {"currency": "USD", "value": amount}}],
    }, batch_id)
    header = body.get("batch_header", {})
    log({"step": "payout", "http": status, "batch_id": header.get("payout_batch_id"),
         "batch_status": header.get("batch_status"), "receiver": email, "amount": amount})
    print(f"HTTP {status} | payout batch {header.get('payout_batch_id')} | status {header.get('batch_status')}")
    if status >= 400:
        print(json.dumps(body, indent=2)[:800])


def payout_status(batch_id):
    status, body = call("GET", f"/v1/payments/payouts/{batch_id}")
    header = body.get("batch_header", {})
    items = [(i.get("transaction_status"), i.get("payout_item", {}).get("receiver")) for i in body.get("items", [])]
    log({"step": "payout_status", "http": status, "batch_id": batch_id,
         "batch_status": header.get("batch_status"), "items": items})
    print(f"HTTP {status} | batch {batch_id} | {header.get('batch_status')} | items {items}")


if __name__ == "__main__":
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    commands = {"order": create_order, "capture": capture, "payout": payout, "payout-status": payout_status}
    if cmd not in commands:
        sys.exit(__doc__)
    commands[cmd](*args)
