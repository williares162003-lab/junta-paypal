"""Tests for Junta's money rules, using a fake PayPal that behaves like the real API (no network)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from junta import core  # noqa: E402
from junta.db import connect  # noqa: E402
from junta.money import to_cents, to_str  # noqa: E402
from junta.paypal import PayPalError  # noqa: E402

ORGANIZER = "organizer@business.example.com"
MEMBERS = [("Rosa", "rosa@personal.example.com"), ("Luis", "luis@personal.example.com"),
           ("Ana", "ana@personal.example.com")]


class FakePayPal:
    """Mimics Orders v2 and Payouts v1, including idempotency by request id."""

    def __init__(self):
        self.orders, self.payouts, self.by_request = {}, {}, {}
        self.capture_calls, self.payout_calls = 0, 0
        self.capture_status = "COMPLETED"
        self.fail_next_payout = False

    def create_order(self, amount_cents, currency, reference, description, return_url, cancel_url, request_id):
        if request_id in self.by_request:
            return self.by_request[request_id]
        self.order_seq = getattr(self, "order_seq", 0) + 1  # PayPal never reuses order ids
        oid = f"ORDER{self.order_seq}"
        self.orders[oid] = {"status": "CREATED", "amount_cents": amount_cents, "currency": currency,
                            "reference": reference}
        result = {"id": oid, "status": "CREATED", "approve_url": f"https://sandbox/approve/{oid}"}
        self.by_request[request_id] = result
        return result

    def approve(self, oid):  # what the member does on PayPal
        self.orders[oid]["status"] = "APPROVED"

    def expire(self, oid):  # PayPal drops orders nobody approved
        del self.orders[oid]

    def get_order(self, oid):
        if oid not in self.orders:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND"})
        o = self.orders[oid]
        return {"id": oid, "status": o["status"], "reference": o["reference"],
                "amount_cents": o["amount_cents"], "currency": o["currency"]}

    def capture_order(self, oid, request_id):
        if request_id in self.by_request:
            return self.by_request[request_id]
        self.capture_calls += 1
        o = self.orders[oid]
        o["status"] = "COMPLETED"
        result = {"order_status": "COMPLETED", "capture_id": f"CAP-{oid}", "capture_status": self.capture_status,
                  "amount_cents": o["amount_cents"], "currency": o["currency"], "payer_email": "x"}
        self.by_request[request_id] = result
        return result

    def create_payout(self, sender_batch_id, receiver_email, amount_cents, currency, note):
        if self.fail_next_payout:
            self.fail_next_payout = False
            raise PayPalError(500, {"name": "INTERNAL_SERVICE_ERROR"})
        if sender_batch_id in self.by_request:
            return self.by_request[sender_batch_id]
        self.payout_calls += 1
        bid = f"BATCH{len(self.payouts) + 1}"
        self.payouts[bid] = {"receiver": receiver_email, "amount_cents": amount_cents, "status": "PENDING"}
        result = {"batch_id": bid, "status": "PENDING"}
        self.by_request[sender_batch_id] = result
        return result

    def get_payout(self, bid):
        return {"batch_id": bid, "status": self.payouts[bid]["status"]}


class MoneyTest(unittest.TestCase):
    def test_cents_round_trip(self):
        self.assertEqual(to_cents("100"), 10000)
        self.assertEqual(to_cents("0.10"), 10)
        self.assertEqual(to_str(10000), "100.00")
        self.assertEqual(to_str(5), "0.05")

    def test_rejects_bad_amounts(self):
        for bad in ("1.005", "abc", "NaN", "Infinity"):
            with self.assertRaises(ValueError):
                to_cents(bad)


class JuntaTest(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.pp = FakePayPal()
        self.gid = core.create_group(self.conn, "Junta de prueba", 10000, ORGANIZER, MEMBERS)
        self.round_id = self.conn.execute("SELECT id FROM rounds WHERE group_id = ?", (self.gid,)).fetchone()[0]

    def tearDown(self):
        self.conn.close()

    def contribution_ids(self, round_id=None):
        return [r[0] for r in self.conn.execute("SELECT id FROM contributions WHERE round_id = ? ORDER BY id",
                                                (round_id or self.round_id,))]

    def pay(self, contribution_id):
        """Full member flow: request the order, approve it on PayPal, come back and confirm."""
        core.request_contribution(self.conn, self.pp, contribution_id, "http://ret", "http://cancel")
        oid = self.conn.execute("SELECT paypal_order_id FROM contributions WHERE id = ?",
                                (contribution_id,)).fetchone()[0]
        self.pp.approve(oid)
        return core.confirm_contribution(self.conn, self.pp, oid)

    def pay_all(self, round_id=None):
        for cid in self.contribution_ids(round_id):
            self.pay(cid)

    # --- setup ---

    def test_group_opens_round_one_with_one_contribution_per_member(self):
        s = core.round_summary(self.conn, self.round_id)
        self.assertEqual((s["round"], s["recipient"], s["expected_cents"]), (1, "Rosa", 30000))
        self.assertEqual(len(s["missing"]), 3)

    def test_duplicate_member_emails_rejected(self):
        with self.assertRaises(core.JuntaError):
            core.create_group(self.conn, "x", 100, ORGANIZER, [("A", "a@x.com"), ("B", "A@x.com")])

    # --- contributions ---

    def test_unapproved_order_is_not_captured(self):
        cid = self.contribution_ids()[0]
        core.request_contribution(self.conn, self.pp, cid, "r", "c")
        oid = self.conn.execute("SELECT paypal_order_id FROM contributions WHERE id = ?", (cid,)).fetchone()[0]
        with self.assertRaises(core.JuntaError):
            core.confirm_contribution(self.conn, self.pp, oid)
        self.assertEqual(self.pp.capture_calls, 0)

    def test_confirming_twice_captures_once(self):
        cid = self.contribution_ids()[0]
        self.assertEqual(self.pay(cid), "captured")
        oid = self.conn.execute("SELECT paypal_order_id FROM contributions WHERE id = ?", (cid,)).fetchone()[0]
        self.assertEqual(core.confirm_contribution(self.conn, self.pp, oid), "already_captured")
        self.assertEqual(self.pp.capture_calls, 1)

    def test_requesting_twice_reuses_the_same_order(self):
        cid = self.contribution_ids()[0]
        core.request_contribution(self.conn, self.pp, cid, "r", "c")
        core.request_contribution(self.conn, self.pp, cid, "r", "c")
        self.assertEqual(len(self.pp.orders), 1)

    def test_expired_order_is_replaced_with_a_new_one(self):
        cid = self.contribution_ids()[0]
        core.request_contribution(self.conn, self.pp, cid, "r", "c")
        old = self.conn.execute("SELECT paypal_order_id FROM contributions WHERE id = ?", (cid,)).fetchone()[0]
        self.pp.expire(old)
        link = core.request_contribution(self.conn, self.pp, cid, "r", "c")
        new = self.conn.execute("SELECT paypal_order_id FROM contributions WHERE id = ?", (cid,)).fetchone()[0]
        self.assertNotEqual(old, new)
        self.assertIn(new, link)
        self.pp.approve(new)
        self.assertEqual(core.confirm_contribution(self.conn, self.pp, new), "captured")

    def test_tampered_order_amount_is_rejected(self):
        cid = self.contribution_ids()[0]
        core.request_contribution(self.conn, self.pp, cid, "r", "c")
        oid = self.conn.execute("SELECT paypal_order_id FROM contributions WHERE id = ?", (cid,)).fetchone()[0]
        self.pp.orders[oid]["amount_cents"] = 100  # someone paid $1 instead of $100
        self.pp.approve(oid)
        with self.assertRaises(core.JuntaError):
            core.confirm_contribution(self.conn, self.pp, oid)

    def test_unknown_order_is_rejected(self):
        with self.assertRaises(core.JuntaError):
            core.confirm_contribution(self.conn, self.pp, "SOMEONE-ELSES-ORDER")

    def test_pending_capture_does_not_count(self):
        self.pp.capture_status = "PENDING"
        with self.assertRaises(core.JuntaError):
            self.pay(self.contribution_ids()[0])
        self.assertEqual(len(core.round_summary(self.conn, self.round_id)["missing"]), 3)

    # --- the pot ---

    def test_pot_blocked_until_everyone_paid(self):
        self.pay(self.contribution_ids()[0])
        with self.assertRaises(core.JuntaError) as ctx:
            core.release_pot(self.conn, self.pp, self.round_id, ORGANIZER)
        self.assertIn("Luis", str(ctx.exception))
        self.assertEqual(self.pp.payout_calls, 0)

    def test_only_organizer_releases_pot(self):
        self.pay_all()
        with self.assertRaises(core.JuntaError):
            core.release_pot(self.conn, self.pp, self.round_id, "rosa@personal.example.com")

    def test_pot_is_sum_of_captures_and_released_once(self):
        self.pay_all()
        self.assertEqual(core.release_pot(self.conn, self.pp, self.round_id, ORGANIZER), "PENDING")
        payout = next(iter(self.pp.payouts.values()))
        self.assertEqual((payout["receiver"], payout["amount_cents"]), ("rosa@personal.example.com", 30000))
        with self.assertRaises(core.JuntaError):
            core.release_pot(self.conn, self.pp, self.round_id, ORGANIZER)
        self.assertEqual(self.pp.payout_calls, 1)

    def test_payout_failure_then_retry_sends_once(self):
        self.pay_all()
        self.pp.fail_next_payout = True
        with self.assertRaises(PayPalError):
            core.release_pot(self.conn, self.pp, self.round_id, ORGANIZER)
        row = self.conn.execute("SELECT status FROM payouts WHERE round_id = ?", (self.round_id,)).fetchone()
        self.assertEqual(row[0], "UNKNOWN")
        core.send_reserved_payout(self.conn, self.pp, self.round_id)
        core.send_reserved_payout(self.conn, self.pp, self.round_id)
        self.assertEqual(self.pp.payout_calls, 1)

    def test_full_junta_rotates_and_ends(self):
        for expected_recipient in ("Rosa", "Luis", "Ana"):
            rid = self.conn.execute("SELECT id FROM rounds WHERE group_id = ? ORDER BY number DESC LIMIT 1",
                                    (self.gid,)).fetchone()[0]
            self.assertEqual(core.round_summary(self.conn, rid)["recipient"], expected_recipient)
            self.pay_all(rid)
            core.release_pot(self.conn, self.pp, rid, ORGANIZER)
            with self.assertRaises(core.JuntaError):  # cannot open next round before the pot arrives
                core.start_next_round(self.conn, self.gid)
            for p in self.pp.payouts.values():
                p["status"] = "SUCCESS"
            self.assertEqual(core.refresh_payout(self.conn, self.pp, rid), "SUCCESS")
            if expected_recipient != "Ana":
                core.start_next_round(self.conn, self.gid)
        with self.assertRaises(core.JuntaError):
            core.start_next_round(self.conn, self.gid)
        total_paid_out = sum(p["amount_cents"] for p in self.pp.payouts.values())
        self.assertEqual(total_paid_out, 3 * 30000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
