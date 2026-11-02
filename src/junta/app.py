"""Junta web app (Flask). Run: .venv/Scripts/python -m junta.app  (from src/) or scripts/run_app.py"""
import hmac
import json
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from . import chat, core, swaps
from .ai import AIClient
from .db import connect
from .money import to_str
from .paypal import PayPal, PayPalError, load_env

ROOT = Path(__file__).resolve().parents[2]
ENV = load_env(ROOT / ".env")

app = Flask(__name__)
app.secret_key = ENV["FLASK_SECRET_KEY"]
app.jinja_env.filters["money"] = to_str
pp = PayPal.from_env(ROOT / ".env")
ai = AIClient.from_env(ENV)  # None -> the organizer works with keyword rules

EVENT_TEXT = {
    "swap_proposed": "Fair turn swap proposed: {description}",
    "swap_applied": "Turn swap approved by everyone affected: {description}",
    "swap_rejected": "Turn swap proposal rejected",
    "swap_stale": "Turn swap cancelled: the round changed before everyone approved",
    "group_created": "Junta created: {members} members, {contribution} {currency} each",
    "round_opened": "Round {round} opened. Pot goes to {recipient}",
    "contribution_requested": "{member} started paying round {round}",
    "contribution_paid": "{member} paid {amount} for round {round}",
    "pot_approved": "Organizer approved the {amount} pot for {recipient}",
    "pot_sent": "Pot sent through PayPal Payouts ({status})",
    "pot_received": "Pot for round {round} delivered",
}


def db():
    return connect(ROOT / "data" / "junta.db")


def current_round_id(conn, group_id):
    row = conn.execute("SELECT id FROM rounds WHERE group_id = ? ORDER BY number DESC LIMIT 1", (group_id,)).fetchone()
    return row["id"] if row else None


def history(conn, group_id, limit=12):
    items = []
    for e in conn.execute("SELECT * FROM events WHERE group_id = ? ORDER BY id DESC LIMIT ?", (group_id, limit)):
        detail = json.loads(e["detail"])
        template = EVENT_TEXT.get(e["kind"], e["kind"])
        try:
            text = template.format(**detail)
        except KeyError:
            text = e["kind"]
        items.append({"at": e["at"], "text": text})
    return items


@app.get("/")
def home():
    conn = db()
    group = conn.execute("SELECT * FROM groups ORDER BY id LIMIT 1").fetchone()
    if group is None:
        return "No junta yet. Run: python scripts/sandbox_round.py setup", 404
    return redirect(url_for("organizer", group_id=group["id"]))


@app.get("/g/<int:group_id>")
def organizer(group_id):
    conn = db()
    group = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone() or abort(404)
    rid = current_round_id(conn, group_id)
    summary = core.round_summary(conn, rid)
    payout = conn.execute("SELECT * FROM payouts WHERE round_id = ?", (rid,)).fetchone()
    members = conn.execute("SELECT * FROM members WHERE group_id = ? ORDER BY turn", (group_id,)).fetchall()
    total_rounds = len(members)
    return render_template("organizer.html", group=group, s=summary, round_id=rid, payout=payout,
                           members=members, total_rounds=total_rounds, history=history(conn, group_id),
                           messages=chat.recent_messages(conn, group_id), proposal=proposal_view(conn, group_id))


@app.get("/m/<int:member_id>")
def member(member_id):
    conn = db()
    m = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone() or abort(404)
    group = conn.execute("SELECT * FROM groups WHERE id = ?", (m["group_id"],)).fetchone()
    rid = current_round_id(conn, group["id"])
    summary = core.round_summary(conn, rid)
    mine = conn.execute("SELECT * FROM contributions WHERE round_id = ? AND member_id = ?", (rid, member_id)).fetchone()
    return render_template("member.html", m=m, group=group, s=summary, mine=mine, history=history(conn, group["id"]),
                           messages=chat.recent_messages(conn, group["id"]),
                           proposal=proposal_view(conn, group["id"], member_id), ai_on=ai is not None)


def proposal_view(conn, group_id, member_id=None):
    prop = swaps.pending_proposal(conn, group_id)
    if prop is None:
        return None
    changes = json.loads(prop["changes"])
    votes = {r["member_id"]: r["accept"] for r in
             conn.execute("SELECT member_id, accept FROM proposal_votes WHERE proposal_id = ?", (prop["id"],))}
    rows = []
    for mid, (old, new) in changes.items():
        name = conn.execute("SELECT name FROM members WHERE id = ?", (int(mid),)).fetchone()["name"]
        rows.append({"member_id": int(mid), "name": name, "old": old, "new": new, "voted": int(mid) in votes})
    return {"id": prop["id"], "rows": rows, "can_vote": member_id is not None and str(member_id) in changes,
            "has_voted": member_id in votes}


@app.post("/m/<int:member_id>/say")
def say(member_id):
    conn = db()
    if conn.execute("SELECT 1 FROM members WHERE id = ?", (member_id,)).fetchone() is None:
        abort(404)
    chat.handle_message(conn, ai, member_id, request.form.get("text", ""))
    return redirect(url_for("member", member_id=member_id) + "#chat")


@app.post("/proposal/<int:proposal_id>/vote")
def vote(proposal_id):
    conn = db()
    member_id = int(request.form.get("member_id", 0))
    try:
        status = swaps.vote(conn, proposal_id, member_id, request.form.get("accept") == "1")
        flash({"APPLIED": "Everyone affected agreed: the turns were swapped.",
               "REJECTED": "The proposal was rejected. Turns stay the same.",
               "STALE": "The round changed before everyone agreed, so the swap was cancelled.",
               "PENDING": "Your vote was recorded. Waiting for the others affected."}[status], "ok")
    except swaps.SwapError as err:
        flash(str(err), "error")
    return redirect(url_for("member", member_id=member_id) + "#chat")


@app.post("/pay/<int:contribution_id>")
def pay(contribution_id):
    conn = db()
    try:
        link = core.request_contribution(conn, pp, contribution_id,
                                         url_for("paypal_return", _external=True),
                                         url_for("paypal_cancel", _external=True))
    except (core.JuntaError, PayPalError) as err:
        flash(str(err), "error")
        return redirect(request.referrer or url_for("home"))
    return redirect(link)


@app.get("/paypal/return")
def paypal_return():
    order_id = request.args.get("token", "")
    conn = db()
    row = conn.execute("SELECT member_id FROM contributions WHERE paypal_order_id = ?", (order_id,)).fetchone()
    try:
        result = core.confirm_contribution(conn, pp, order_id)
        flash("Payment confirmed by PayPal. Thank you!" if result == "captured" else "Already paid.", "ok")
    except (core.JuntaError, PayPalError) as err:
        flash(f"Payment not confirmed: {err}", "error")
    return redirect(url_for("member", member_id=row["member_id"]) if row else url_for("home"))


@app.get("/paypal/cancel")
def paypal_cancel():
    flash("Payment cancelled. You can try again anytime.", "error")
    return redirect(url_for("home"))


def organizer_pin_ok():
    return hmac.compare_digest(request.form.get("pin", ""), ENV.get("ORGANIZER_PIN", "\0"))


@app.post("/g/<int:group_id>/release")
def release(group_id):
    conn = db()
    if not organizer_pin_ok():
        flash("Wrong organizer PIN. The pot was not released.", "error")
        return redirect(url_for("organizer", group_id=group_id))
    group = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    try:
        status = core.release_pot(conn, pp, current_round_id(conn, group_id), group["organizer_email"])
        flash(f"Pot sent through PayPal Payouts. Status: {status}", "ok")
    except (core.JuntaError, PayPalError) as err:
        flash(str(err), "error")
    return redirect(url_for("organizer", group_id=group_id))


@app.post("/g/<int:group_id>/refresh")
def refresh(group_id):
    conn = db()
    try:
        flash(f"Payout status: {core.refresh_payout(conn, pp, current_round_id(conn, group_id))}", "ok")
    except (core.JuntaError, PayPalError) as err:
        flash(str(err), "error")
    return redirect(url_for("organizer", group_id=group_id))


@app.post("/g/<int:group_id>/next")
def next_round(group_id):
    conn = db()
    if not organizer_pin_ok():
        flash("Wrong organizer PIN.", "error")
    else:
        try:
            core.start_next_round(conn, group_id)
            flash("Next round opened.", "ok")
        except core.JuntaError as err:
            flash(str(err), "error")
    return redirect(url_for("organizer", group_id=group_id))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
