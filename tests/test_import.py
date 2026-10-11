import os, tempfile, shutil, json, asyncio
tmp = tempfile.mkdtemp()
os.environ.update(DASHBOARD_DATA_DIR=tmp)
os.environ.pop("GOOGLE_CLIENT_ID", None)
rows = [{"id": f"id{i}", "symbol": f"SYM{i}", "source": "user" if i % 2 else "system", "as_of": "2026-10-09",
         "entry": 100.0, "stop_loss": 90.0, "target": 120.0, "score": "4/4", "hold_days": 10, "taken": False} for i in range(8)]
json.dump(rows, open(f"{tmp}/suggestions_log.json", "w"))
json.dump({"default_leverage": 3.0, "stocks_text": "TCS 2x"}, open(f"{tmp}/funding_rules.json", "w"))
import db, tracker, funding, import_legacy
import_legacy.main("owner@example.com"); import_legacy.main("owner@example.com")      # twice: idempotent
with db.SessionLocal() as s: uid = s.query(db.User).filter_by(email="owner@example.com").one().id
rows = asyncio.run(tracker.list_entries(uid))
assert len(rows) == 8 and {r["source"] for r in rows} == {"user", "system"}, len(rows)
assert funding.load_rules(uid)["default_leverage"] == 3.0
assert asyncio.run(tracker.list_entries(uid + 1)) == []                                   # nobody else inherits it
print("import test passed")
