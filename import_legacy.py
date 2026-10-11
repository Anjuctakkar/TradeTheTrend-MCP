#!/usr/bin/env python3
"""One-time import of the old single-owner files into one user's account.

    python import_legacy.py you@gmail.com

Reads suggestions_log.json and funding_rules.json from the data folder (DASHBOARD_DATA_DIR, or next to the
code) and stores them under that user. If the user hasn't signed in yet, a placeholder account is created and
is picked up by email when they first sign in with Google. Safe to run twice: entries already imported are skipped.
Nobody else gets this history: new users always start empty.
"""
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

# Read .env beside this file (real environment variables win). Docker passes its settings as variables instead.
load_dotenv(Path(__file__).resolve().parent / ".env", encoding="utf-8-sig")

import auth  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import funding  # noqa: E402
import tracker  # noqa: E402
import user_store  # noqa: E402


def main(email: str) -> None:
    email = email.strip().lower()
    db.init_db()
    with db.SessionLocal() as s:
        u = s.query(db.User).filter_by(email=email).one_or_none()
        if u is None:
            u = db.User(email=email, role="admin" if email in auth.ADMIN_EMAILS else "user", is_active=True, session_version=1)
            s.add(u)
            s.commit()
        uid = u.id

    log = config.DATA_DIR / "suggestions_log.json"
    if log.exists():
        rows = json.loads(log.read_text(encoding="utf-8"))
        added = 0
        for source in ("user", "system"):
            items = [r for r in rows if r.get("source", "system") == source]
            if items:
                added += asyncio.run(tracker.add_items(items, source, uid))["added"]
        print(f"Watchlist: {added} of {len(rows)} entries imported.")
    else:
        print("No suggestions_log.json found; skipped.")

    rules_file = config.DATA_DIR / "funding_rules.json"
    if rules_file.exists():
        rules, errors = funding.validate(json.loads(rules_file.read_text(encoding="utf-8")))
        if errors:
            print("Funding rules skipped (invalid):", " ".join(errors[:4]))
        else:
            funding.save_rules(rules, uid)
            print("Funding rules imported.")
    else:
        print("No funding_rules.json found; skipped.")


if __name__ == "__main__":
    if len(sys.argv) != 2 or "@" not in sys.argv[1]:
        sys.exit("Usage: python import_legacy.py you@gmail.com")
    main(sys.argv[1])
