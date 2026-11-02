"""Small PayPal REST client (sandbox only). Standard library, no SDK.

Only these operations are used:
  - Orders v2: create, get, capture   (a member pays their contribution and approves it on PayPal)
  - Payouts v1: create, get           (the organizer sends the pot to the round's recipient)
"""
import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from .money import to_cents, to_str


class PayPalError(Exception):
    def __init__(self, status, body):
        super().__init__(f"PayPal HTTP {status}: {json.dumps(body)[:300]}")
        self.status, self.body = status, body


def load_env(path):
    env = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


class PayPal:
    def __init__(self, client_id, secret, base_url):
        if "sandbox" not in base_url:
            raise ValueError("Junta is a sandbox prototype: PAYPAL_BASE_URL must point to the sandbox.")
        self.client_id, self.secret, self.base = client_id, secret, base_url.rstrip("/")
        self._token, self._token_expires = None, 0.0

    @classmethod
    def from_env(cls, path):
        env = load_env(path)
        return cls(env["PAYPAL_CLIENT_ID"], env["PAYPAL_CLIENT_SECRET"], env["PAYPAL_BASE_URL"])

    # ---------- HTTP ----------

    def _access_token(self):
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        auth = base64.b64encode(f"{self.client_id}:{self.secret}".encode()).decode()
        req = urllib.request.Request(f"{self.base}/v1/oauth2/token", data=b"grant_type=client_credentials",
                                     headers={"Authorization": f"Basic {auth}",
                                              "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        self._token, self._token_expires = data["access_token"], time.time() + data["expires_in"]
        return self._token

    def _call(self, method, path, body=None, request_id=None):
        headers = {"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id  # a retry with the same id is not executed twice
        data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as err:
            raise PayPalError(err.code, json.loads(err.read() or b"{}")) from None

    # ---------- Orders: contributions ----------

    def create_order(self, amount_cents, currency, reference, description, return_url, cancel_url, request_id):
        body = self._call("POST", "/v2/checkout/orders", {
            "intent": "CAPTURE",
            "purchase_units": [{"reference_id": reference, "custom_id": reference, "description": description,
                                "amount": {"currency_code": currency, "value": to_str(amount_cents)}}],
            "application_context": {"brand_name": "Junta (sandbox)", "user_action": "PAY_NOW",
                                    "shipping_preference": "NO_SHIPPING",
                                    "return_url": return_url, "cancel_url": cancel_url},
        }, request_id)
        approve = next((l["href"] for l in body.get("links", []) if l.get("rel") in ("approve", "payer-action")), None)
        return {"id": body["id"], "status": body["status"], "approve_url": approve}

    def get_order(self, order_id):
        body = self._call("GET", f"/v2/checkout/orders/{order_id}")
        unit = (body.get("purchase_units") or [{}])[0]
        return {"id": body["id"], "status": body["status"], "reference": unit.get("reference_id"),
                "amount_cents": to_cents(unit["amount"]["value"]), "currency": unit["amount"]["currency_code"]}

    def capture_order(self, order_id, request_id):
        body = self._call("POST", f"/v2/checkout/orders/{order_id}/capture", None, request_id)
        capture = body["purchase_units"][0]["payments"]["captures"][0]
        return {"order_status": body["status"], "capture_id": capture["id"], "capture_status": capture["status"],
                "amount_cents": to_cents(capture["amount"]["value"]), "currency": capture["amount"]["currency_code"],
                "payer_email": body.get("payer", {}).get("email_address")}

    # ---------- Payouts: the pot ----------

    def create_payout(self, sender_batch_id, receiver_email, amount_cents, currency, note):
        body = self._call("POST", "/v1/payments/payouts", {
            "sender_batch_header": {"sender_batch_id": sender_batch_id, "email_subject": "Junta: your pot"},
            "items": [{"recipient_type": "EMAIL", "receiver": receiver_email, "note": note,
                       "sender_item_id": f"{sender_batch_id}-1",
                       "amount": {"currency": currency, "value": to_str(amount_cents)}}],
        }, sender_batch_id)
        header = body["batch_header"]
        return {"batch_id": header["payout_batch_id"], "status": header["batch_status"]}

    def get_payout(self, batch_id):
        body = self._call("GET", f"/v1/payments/payouts/{batch_id}")
        return {"batch_id": batch_id, "status": body["batch_header"]["batch_status"]}
