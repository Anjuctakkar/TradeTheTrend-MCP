#!/usr/bin/env python3
"""One-time import of your old funding rules into one user's account.

    python import_legacy.py you@gmail.com

Reads funding_rules.json from the data folder (DASHBOARD_DATA_DIR, or next to the code) and stores it under
that user. If the user hasn't signed in yet, a placeholder account is created and picked up by email when
they first sign in with Google. Safe to run twice.

Your old EVALUATION list (suggestions_log.json) is no longer stored on the server. Open the app, go to the
Evaluation tab, choose "Open my file" and pick that file: it is read in your browser and kept on your computer.
"""
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

    if (config.DATA_DIR / "suggestions_log.json").exists():
        print("suggestions_log.json found: it is not imported here. In the app, Evaluation tab > Open my file, and choose it.")

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
