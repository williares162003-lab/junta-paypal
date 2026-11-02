"""Run one real Junta round on the PayPal sandbox using the core rules (before the web app exists).

  python scripts/sandbox_round.py setup        # create a 2-member test junta ($5 each) in data/junta.db
  python scripts/sandbox_round.py links        # print the approval link of every unpaid contribution
  python scripts/sandbox_round.py confirm      # capture every contribution the members approved
  python scripts/sandbox_round.py release      # organizer releases the pot (only if everyone paid)
  python scripts/sandbox_round.py refresh      # update the payout status from PayPal
  python scripts/sandbox_round.py summary
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from junta import core  # noqa: E402
from junta.db import connect  # noqa: E402
from junta.money import to_str  # noqa: E402
from junta.paypal import PayPal, load_env  # noqa: E402

ENV = load_env(ROOT / ".env")
conn = connect(ROOT / "data" / "junta.db")
pp = PayPal.from_env(ROOT / ".env")
RETURN, CANCEL = "http://localhost:5000/paypal/return", "http://localhost:5000/paypal/cancel"


def current_round():
    row = conn.execute("SELECT id FROM rounds ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        sys.exit("No junta yet. Run: setup")
    return row[0]


def summary():
    s = core.round_summary(conn, current_round())
    print(f"Round {s['round']} | {s['status']} | recipient {s['recipient']} | "
          f"captured {to_str(s['captured_cents'])} of {to_str(s['expected_cents'])} {s['currency']}")
    for c in s["contributions"]:
        print(f"  - {c['name']:8s} {c['status']}")
    for e in conn.execute("SELECT at, kind, detail FROM events ORDER BY id DESC LIMIT 5").fetchall()[::-1]:
        print(f"  [{e['at']}] {e['kind']} {e['detail']}")


def main(cmd):
    if cmd == "setup":
        if conn.execute("SELECT 1 FROM groups").fetchone():
            sys.exit("A test junta already exists in data/junta.db (delete the file to start over).")
        emails = [e.strip() for e in ENV["MEMBER_EMAILS"].split(",") if e.strip()][:2]
        core.create_group(conn, "Junta de prueba (sandbox)", 500, ENV["ORGANIZER_EMAIL"],
                          [("Rosa", emails[0]), ("Luis", emails[1])])
    elif cmd == "links":
        for c in core.round_summary(conn, current_round())["contributions"]:
            if c["status"] != "CAPTURED":
                print(f"{c['name']}: {core.request_contribution(conn, pp, c['id'], RETURN, CANCEL)}")
    elif cmd == "confirm":
        for row in conn.execute("SELECT paypal_order_id FROM contributions WHERE status = 'ORDER_CREATED'"):
            try:
                print(row[0], core.confirm_contribution(conn, pp, row[0]))
            except core.JuntaError as err:
                print(row[0], "->", err)
    elif cmd == "release":
        print("payout:", core.release_pot(conn, pp, current_round(), ENV["ORGANIZER_EMAIL"]))
    elif cmd == "refresh":
        print("payout:", core.refresh_payout(conn, pp, current_round()))
    elif cmd != "summary":
        sys.exit(__doc__)
    summary()


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "")
    except core.JuntaError as err:
        sys.exit(f"Rule: {err}")
