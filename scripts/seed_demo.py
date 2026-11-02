"""Create the demo junta (4 members, $100 each) used for the video. Prints its URLs.

Members use the sandbox personal accounts from .env (MEMBER_EMAILS); missing ones get example addresses.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from junta import core  # noqa: E402
from junta.db import connect  # noqa: E402
from junta.paypal import load_env  # noqa: E402

env = load_env(ROOT / ".env")
emails = [e.strip() for e in env["MEMBER_EMAILS"].split(",") if e.strip()]
names = ["Ana", "Rosa", "Luis", "Carmen"]
emails += [f"{n.lower()}@personal.example.com" for n in names[len(emails):]]
conn = connect(ROOT / "data" / "junta.db")
gid = core.create_group(conn, "Junta de la familia", 10000, env["ORGANIZER_EMAIL"], list(zip(names, emails)))
print(f"Organizer: http://localhost:5000/g/{gid}")
for row in conn.execute("SELECT id, name FROM members WHERE group_id = ? ORDER BY turn", (gid,)):
    print(f"{row['name']}: http://localhost:5000/m/{row['id']}")
