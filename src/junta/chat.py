"""Group chat: a member writes, the organizer bot understands it and answers with facts computed by code."""
import json

from . import core, organizer, swaps
from .money import to_str


def _post(conn, group_id, member_id, text, intent=None, source=None):
    conn.execute("INSERT INTO messages (group_id, member_id, text, intent, source) VALUES (?, ?, ?, ?, ?)",
                 (group_id, member_id, text, intent, source))


def _facts(conn, member):
    rnd = swaps.current_round(conn, member["group_id"])
    s = core.round_summary(conn, rnd["id"])
    mine = conn.execute("SELECT status FROM contributions WHERE round_id = ? AND member_id = ?",
                        (rnd["id"], member["id"])).fetchone()
    return {"member": member["name"], "member_turn": member["turn"], "round": s["round"],
            "round_recipient": s["recipient"], "contribution": to_str(s["expected_cents"] // len(s["contributions"])),
            "currency": s["currency"], "pot_collected": to_str(s["captured_cents"]),
            "pot_total": to_str(s["expected_cents"]), "still_to_pay": s["missing"],
            "member_paid_this_round": mine["status"] == "CAPTURED" if mine else False}


def handle_message(conn, ai, member_id, text):
    """Store the member's message, understand it, act on preferences, and post the bot's reply."""
    text = text.strip()[:1000]
    if not text:
        return None
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    group_id = member["group_id"]
    result, source = organizer.classify(ai, text)
    intent = result["intent"]
    _post(conn, group_id, member_id, text, intent, source)

    facts = _facts(conn, member)
    name = member["name"]
    missing = ", ".join(facts["still_to_pay"]) or "nadie"

    if intent in ("needs_pot_early", "prefers_later"):
        if member["turn"] < swaps.open_turns_from(conn, group_id):
            template = f"{name}, tú ya recibiste el pozo en el turno {member['turn']}, así que tu turno ya no se puede mover."
        else:
            pref = "EARLY" if intent == "needs_pot_early" else "LATER"
            swaps.set_preference(conn, member_id, pref, result["urgency"], result["reason"])
            prop = swaps.maybe_propose(conn, group_id)
            if prop:
                changes = json.loads(prop["changes"])
                facts["swap_proposal"] = swaps.describe(conn, changes)
                facts["must_approve"] = swaps.affected_names(conn, changes)
                template = (f"Entendido, {name}. Propuesta de cambio justo: {facts['swap_proposal']}. "
                            f"Solo se aplica si lo aprueban {', '.join(facts['must_approve'])}.")
            elif pref == "EARLY":
                facts["swap_proposal"] = None
                template = (f"Anoté que necesitas cobrar antes, {name}. Por ahora nadie ofreció cobrar más tarde; "
                            "si alguien puede esperar, que lo escriba aquí y propongo un cambio justo.")
            else:
                facts["swap_proposal"] = None
                template = (f"Gracias, {name}. Anoté que puedes cobrar más tarde. Si alguien necesita el dinero "
                            "antes, te propondré un cambio justo.")
    elif intent == "late_payment":
        template = (f"Gracias por avisar, {name}. El pozo de la ronda {facts['round']} se entrega solo cuando todos "
                    f"pagan (falta: {missing}). Si es una emergencia y necesitas el dinero antes, dilo y busco un "
                    "cambio de turno justo.")
    elif intent == "question":
        template = (f"Ronda {facts['round']}: el pozo es para {facts['round_recipient']}. Van "
                    f"{facts['pot_collected']} de {facts['pot_total']} {facts['currency']}; falta: {missing}. "
                    f"Tu turno es el {facts['member_turn']}.")
    elif intent == "greeting":
        template = f"¡Hola, {name}! Aquí estoy para ayudar con pagos, turnos y cambios de turno."
    else:
        template = ("Solo puedo ayudar con pagos, turnos y cambios de turno justos. Las reglas del pozo no cambian "
                    "por chat: se entrega cuando todos pagan y lo libera el organizador.")

    reply, reply_source = organizer.compose_reply(ai, text, facts, template)
    _post(conn, group_id, None, reply, intent, reply_source)
    return {"intent": intent, "understood_by": source, "reply": reply, "reply_by": reply_source}


def recent_messages(conn, group_id, limit=40):
    rows = conn.execute("""SELECT msg.*, m.name FROM messages msg LEFT JOIN members m ON m.id = msg.member_id
                           WHERE msg.group_id = ? ORDER BY msg.id DESC LIMIT ?""", (group_id, limit)).fetchall()
    return [dict(r) for r in rows][::-1]
