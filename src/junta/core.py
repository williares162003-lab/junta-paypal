"""Junta's money rules. Deterministic code: the AI never decides amounts, recipients or releases.

Rules enforced here (and backed by UNIQUE/CHECK constraints in db.py):
  1. Every member, including the round's recipient, owes exactly one contribution per round.
  2. A contribution counts only when PayPal confirms a COMPLETED capture for the exact amount and currency,
     on the order created for that contribution.
  3. The pot is released only when every contribution of the round is captured, only by the organizer,
     and at most once per round. Its amount is the sum of the confirmed captures, never an outside number.
  4. A payout is reserved in the database before PayPal is called, so retries reuse the same batch id.
"""
from .db import log_event
from .money import to_str
from .paypal import PayPalError

SANDBOX_CHECKOUT = "https://www.sandbox.paypal.com/checkoutnow?token={}"


class JuntaError(Exception):
    """A rule was violated. Nothing was changed."""


# ---------- setup ----------

def create_group(conn, name, contribution_cents, organizer_email, members, currency="USD"):
    """members: list of (name, paypal_email) in turn order. Opens round 1."""
    if len(members) < 2:
        raise JuntaError("A junta needs at least 2 members.")
    if len({email.lower() for _, email in members}) != len(members):
        raise JuntaError("Each member needs a different PayPal email.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        group_id = conn.execute(
            "INSERT INTO groups (name, contribution_cents, currency, organizer_email) VALUES (?, ?, ?, ?)",
            (name, contribution_cents, currency, organizer_email)).lastrowid
        for turn, (member_name, email) in enumerate(members, start=1):
            conn.execute("INSERT INTO members (group_id, name, paypal_email, turn) VALUES (?, ?, ?, ?)",
                         (group_id, member_name, email, turn))
        log_event(conn, group_id, "group_created", name=name, members=len(members),
                  contribution=to_str(contribution_cents), currency=currency)
        _open_round(conn, group_id, 1)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return group_id


def _open_round(conn, group_id, number):
    group = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    recipient = conn.execute("SELECT * FROM members WHERE group_id = ? AND turn = ?", (group_id, number)).fetchone()
    if recipient is None:
        raise JuntaError("The junta is complete: every member already had their turn.")
    round_id = conn.execute("INSERT INTO rounds (group_id, number, recipient_id) VALUES (?, ?, ?)",
                            (group_id, number, recipient["id"])).lastrowid
    for member in conn.execute("SELECT id FROM members WHERE group_id = ?", (group_id,)).fetchall():
        conn.execute("INSERT INTO contributions (round_id, member_id, amount_cents) VALUES (?, ?, ?)",
                     (round_id, member["id"], group["contribution_cents"]))
    log_event(conn, group_id, "round_opened", round=number, recipient=recipient["name"])
    return round_id


def start_next_round(conn, group_id):
    conn.execute("BEGIN IMMEDIATE")
    try:
        last = conn.execute("SELECT * FROM rounds WHERE group_id = ? ORDER BY number DESC LIMIT 1",
                            (group_id,)).fetchone()
        if last["status"] != "PAID_OUT":
            raise JuntaError(f"Round {last['number']} is not paid out yet.")
        round_id = _open_round(conn, group_id, last["number"] + 1)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return round_id


# ---------- contributions ----------

def _reference(contribution_id):
    return f"junta-contribution-{contribution_id}"


def request_contribution(conn, pp, contribution_id, return_url, cancel_url):
    """Create (or reuse) the PayPal order for one contribution. Returns the approval link for the member."""
    row = conn.execute("""SELECT c.*, r.number, r.group_id, g.currency, g.name AS group_name, m.name AS member
                          FROM contributions c JOIN rounds r ON r.id = c.round_id
                          JOIN groups g ON g.id = r.group_id JOIN members m ON m.id = c.member_id
                          WHERE c.id = ?""", (contribution_id,)).fetchone()
    if row is None:
        raise JuntaError("Unknown contribution.")
    if row["status"] == "CAPTURED":
        raise JuntaError("This contribution is already paid.")
    if row["status"] == "ORDER_CREATED":
        # Reuse the existing order only while PayPal still has it open. Unapproved orders expire.
        try:
            if pp.get_order(row["paypal_order_id"])["status"] in ("CREATED", "APPROVED", "PAYER_ACTION_REQUIRED"):
                return SANDBOX_CHECKOUT.format(row["paypal_order_id"])
        except PayPalError as err:
            if err.status != 404:
                raise
    attempt = row["order_attempts"] + 1
    order = pp.create_order(row["amount_cents"], row["currency"], _reference(contribution_id),
                            f"{row['group_name']} - round {row['number']} - {row['member']}",
                            return_url, cancel_url, request_id=f"junta-order-{contribution_id}-{attempt}")
    conn.execute("""UPDATE contributions SET status = 'ORDER_CREATED', paypal_order_id = ?, order_attempts = ?,
                    updated_at = datetime('now') WHERE id = ? AND status IN ('DUE', 'FAILED', 'ORDER_CREATED')""",
                 (order["id"], attempt, contribution_id))
    log_event(conn, row["group_id"], "contribution_requested", round=row["number"], member=row["member"],
              order_id=order["id"])
    return order["approve_url"] or SANDBOX_CHECKOUT.format(order["id"])


def confirm_contribution(conn, pp, order_id):
    """Called when PayPal sends the member back. Captures the approved order after checking it is ours."""
    row = conn.execute("""SELECT c.*, r.number, r.group_id, g.currency, m.name AS member
                          FROM contributions c JOIN rounds r ON r.id = c.round_id
                          JOIN groups g ON g.id = r.group_id JOIN members m ON m.id = c.member_id
                          WHERE c.paypal_order_id = ?""", (order_id,)).fetchone()
    if row is None:
        raise JuntaError("This PayPal order does not belong to any contribution.")
    if row["status"] == "CAPTURED":
        return "already_captured"

    order = pp.get_order(order_id)
    if order["reference"] != _reference(row["id"]):
        raise JuntaError("Order reference does not match the contribution.")
    if (order["amount_cents"], order["currency"]) != (row["amount_cents"], row["currency"]):
        raise JuntaError("Order amount or currency does not match the contribution.")
    if order["status"] != "APPROVED":
        raise JuntaError(f"The member has not approved the payment yet (order is {order['status']}).")

    capture = pp.capture_order(order_id, request_id=f"junta-capture-{order_id}")
    if capture["capture_status"] != "COMPLETED":
        raise JuntaError(f"PayPal capture is {capture['capture_status']}, not COMPLETED. It will be retried.")
    if (capture["amount_cents"], capture["currency"]) != (row["amount_cents"], row["currency"]):
        raise JuntaError("Captured amount differs from the contribution. Needs manual review.")

    conn.execute("""UPDATE contributions SET status = 'CAPTURED', paypal_capture_id = ?, updated_at = datetime('now')
                    WHERE id = ? AND status = 'ORDER_CREATED'""", (capture["capture_id"], row["id"]))
    log_event(conn, row["group_id"], "contribution_paid", round=row["number"], member=row["member"],
              amount=to_str(row["amount_cents"]), capture_id=capture["capture_id"])
    return "captured"


# ---------- the pot ----------

def round_summary(conn, round_id):
    rnd = conn.execute("""SELECT r.*, m.name AS recipient, m.paypal_email AS recipient_email, g.currency,
                                 g.organizer_email, g.contribution_cents
                          FROM rounds r JOIN members m ON m.id = r.recipient_id JOIN groups g ON g.id = r.group_id
                          WHERE r.id = ?""", (round_id,)).fetchone()
    if rnd is None:
        raise JuntaError("Unknown round.")
    rows = conn.execute("""SELECT c.id, c.status, c.amount_cents, m.name FROM contributions c
                           JOIN members m ON m.id = c.member_id WHERE c.round_id = ? ORDER BY m.turn""",
                        (round_id,)).fetchall()
    captured = sum(r["amount_cents"] for r in rows if r["status"] == "CAPTURED")
    return {"round": rnd["number"], "status": rnd["status"], "recipient": rnd["recipient"],
            "recipient_email": rnd["recipient_email"], "currency": rnd["currency"],
            "organizer_email": rnd["organizer_email"], "group_id": rnd["group_id"],
            "expected_cents": rnd["contribution_cents"] * len(rows), "captured_cents": captured,
            "missing": [r["name"] for r in rows if r["status"] != "CAPTURED"],
            "contributions": [dict(r) for r in rows]}


def release_pot(conn, pp, round_id, approved_by):
    """Organizer releases the pot. Reserves the payout first, then calls PayPal."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        s = round_summary(conn, round_id)
        if approved_by.lower() != s["organizer_email"].lower():
            raise JuntaError("Only the organizer can release the pot.")
        if s["status"] != "COLLECTING":
            raise JuntaError("The pot for this round was already released.")
        if s["missing"]:
            raise JuntaError("Not everyone has paid yet: " + ", ".join(s["missing"]))
        if s["captured_cents"] != s["expected_cents"]:
            raise JuntaError("Confirmed contributions do not add up to the expected pot. Needs review.")
        batch = f"junta-g{s['group_id']}-r{s['round']}"
        conn.execute("""INSERT INTO payouts (round_id, recipient_email, amount_cents, sender_batch_id, approved_by)
                        VALUES (?, ?, ?, ?, ?)""",
                     (round_id, s["recipient_email"], s["captured_cents"], batch, approved_by))
        conn.execute("UPDATE rounds SET status = 'PAYING_OUT' WHERE id = ?", (round_id,))
        log_event(conn, s["group_id"], "pot_approved", round=s["round"], recipient=s["recipient"],
                  amount=to_str(s["captured_cents"]), approved_by=approved_by)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return send_reserved_payout(conn, pp, round_id)


def send_reserved_payout(conn, pp, round_id):
    """Send (or safely re-send) a reserved payout. The same sender_batch_id is reused on every retry."""
    payout = conn.execute("SELECT * FROM payouts WHERE round_id = ?", (round_id,)).fetchone()
    if payout is None:
        raise JuntaError("No approved payout for this round.")
    if payout["paypal_batch_id"]:
        return refresh_payout(conn, pp, round_id)
    s = round_summary(conn, round_id)
    try:
        result = pp.create_payout(payout["sender_batch_id"], payout["recipient_email"], payout["amount_cents"],
                                  s["currency"], f"Junta round {s['round']} pot")
    except PayPalError:
        conn.execute("UPDATE payouts SET status = 'UNKNOWN', updated_at = datetime('now') WHERE id = ?",
                     (payout["id"],))
        raise
    conn.execute("""UPDATE payouts SET paypal_batch_id = ?, status = ?, updated_at = datetime('now')
                    WHERE id = ?""", (result["batch_id"], result["status"], payout["id"]))
    log_event(conn, s["group_id"], "pot_sent", round=s["round"], batch_id=result["batch_id"],
              status=result["status"])
    return result["status"]


def refresh_payout(conn, pp, round_id):
    payout = conn.execute("SELECT * FROM payouts WHERE round_id = ?", (round_id,)).fetchone()
    result = pp.get_payout(payout["paypal_batch_id"])
    conn.execute("UPDATE payouts SET status = ?, updated_at = datetime('now') WHERE id = ?",
                 (result["status"], payout["id"]))
    if result["status"] == "SUCCESS":
        rnd = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if rnd["status"] != "PAID_OUT":
            conn.execute("UPDATE rounds SET status = 'PAID_OUT' WHERE id = ?", (round_id,))
            log_event(conn, rnd["group_id"], "pot_received", round=rnd["number"], batch_id=payout["paypal_batch_id"])
    return result["status"]
