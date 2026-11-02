"""Turn-swap proposals: computed by fairswap.propose, approved by every affected member, applied atomically."""
import json

from .db import log_event
from .fairswap import propose


class SwapError(Exception):
    pass


def current_round(conn, group_id):
    return conn.execute("SELECT * FROM rounds WHERE group_id = ? ORDER BY number DESC LIMIT 1", (group_id,)).fetchone()


def open_turns_from(conn, group_id):
    """First turn that can still change: the current round if its pot was not approved yet, else the next one."""
    rnd = current_round(conn, group_id)
    has_payout = conn.execute("SELECT 1 FROM payouts WHERE round_id = ?", (rnd["id"],)).fetchone()
    return rnd["number"] if rnd["status"] == "COLLECTING" and not has_payout else rnd["number"] + 1


def waiting_members(conn, group_id):
    first = open_turns_from(conn, group_id)
    rows = conn.execute("""SELECT m.id, m.name, m.turn, COALESCE(p.preference, 'NEUTRAL') AS preference,
                                  COALESCE(p.urgency, 0) AS urgency
                           FROM members m LEFT JOIN preferences p ON p.member_id = m.id
                           WHERE m.group_id = ? AND m.turn >= ? ORDER BY m.turn""", (group_id, first)).fetchall()
    return [dict(r) for r in rows]


def set_preference(conn, member_id, preference, urgency=0, reason=""):
    conn.execute("""INSERT INTO preferences (member_id, preference, urgency, reason) VALUES (?, ?, ?, ?)
                    ON CONFLICT(member_id) DO UPDATE SET preference = excluded.preference, urgency = excluded.urgency,
                    reason = excluded.reason, updated_at = datetime('now')""",
                 (member_id, preference, urgency, reason))


def pending_proposal(conn, group_id):
    return conn.execute("SELECT * FROM proposals WHERE group_id = ? AND status = 'PENDING' ORDER BY id DESC LIMIT 1",
                        (group_id,)).fetchone()


def maybe_propose(conn, group_id):
    """Create a proposal if the fair assignment differs from the current one. Returns the proposal row or None."""
    existing = pending_proposal(conn, group_id)
    if existing:
        return existing
    members = waiting_members(conn, group_id)
    if len(members) < 2:
        return None
    result = propose(members)
    if not result["changes"]:
        return None
    changes = {str(mid): list(turns) for mid, turns in result["changes"].items()}
    pid = conn.execute("INSERT INTO proposals (group_id, changes) VALUES (?, ?)",
                       (group_id, json.dumps(changes))).lastrowid
    log_event(conn, group_id, "swap_proposed", proposal=pid, description=describe(conn, changes))
    return conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()


def describe(conn, changes):
    parts = []
    for mid, (old, new) in changes.items():
        name = conn.execute("SELECT name FROM members WHERE id = ?", (int(mid),)).fetchone()["name"]
        parts.append(f"{name}: turno {old} → {new}")
    return "; ".join(parts)


def affected_names(conn, changes):
    return [conn.execute("SELECT name FROM members WHERE id = ?", (int(mid),)).fetchone()["name"] for mid in changes]


def vote(conn, proposal_id, member_id, accept):
    """Record one affected member's decision. Applies the swap when everyone affected accepted."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        prop = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if prop is None or prop["status"] != "PENDING":
            raise SwapError("This proposal is no longer open.")
        changes = json.loads(prop["changes"])
        if str(member_id) not in changes:
            raise SwapError("Only members whose turn changes can vote on this proposal.")
        conn.execute("""INSERT INTO proposal_votes (proposal_id, member_id, accept) VALUES (?, ?, ?)
                        ON CONFLICT(proposal_id, member_id) DO UPDATE SET accept = excluded.accept, at = datetime('now')""",
                     (proposal_id, member_id, 1 if accept else 0))
        votes = {str(r["member_id"]): r["accept"] for r in
                 conn.execute("SELECT member_id, accept FROM proposal_votes WHERE proposal_id = ?", (proposal_id,))}
        if not accept:
            conn.execute("UPDATE proposals SET status = 'REJECTED' WHERE id = ?", (proposal_id,))
            log_event(conn, prop["group_id"], "swap_rejected", proposal=proposal_id)
            status = "REJECTED"
        elif all(votes.get(mid) == 1 for mid in changes):
            status = _apply(conn, prop, changes)
        else:
            status = "PENDING"
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return status


def _apply(conn, prop, changes):
    group_id = prop["group_id"]
    first_open = open_turns_from(conn, group_id)
    for mid, (old, new) in changes.items():
        row = conn.execute("SELECT turn FROM members WHERE id = ?", (int(mid),)).fetchone()
        if row["turn"] != old or old < first_open or new < first_open:
            conn.execute("UPDATE proposals SET status = 'STALE' WHERE id = ?", (prop["id"],))
            log_event(conn, group_id, "swap_stale", proposal=prop["id"])
            return "STALE"
    # Two steps so the UNIQUE(group_id, turn) constraint never sees a duplicate turn.
    for mid, (old, new) in changes.items():
        conn.execute("UPDATE members SET turn = ? WHERE id = ?", (new + 100000, int(mid)))
    for mid, (old, new) in changes.items():
        conn.execute("UPDATE members SET turn = ? WHERE id = ?", (new, int(mid)))
        set_preference(conn, int(mid), "NEUTRAL", 0, "swap applied")
    rnd = current_round(conn, group_id)
    if rnd["status"] == "COLLECTING" and rnd["number"] >= first_open:
        new_recipient = conn.execute("SELECT id FROM members WHERE group_id = ? AND turn = ?",
                                     (group_id, rnd["number"])).fetchone()["id"]
        conn.execute("UPDATE rounds SET recipient_id = ? WHERE id = ?", (new_recipient, rnd["id"]))
    conn.execute("UPDATE proposals SET status = 'APPLIED' WHERE id = ?", (prop["id"],))
    log_event(conn, group_id, "swap_applied", proposal=prop["id"], description=describe(conn, changes))
    return "APPLIED"
