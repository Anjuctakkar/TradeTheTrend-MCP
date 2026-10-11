import os, tempfile, json
tmp = tempfile.mkdtemp()
os.environ.update(DASHBOARD_DATA_DIR=tmp)
os.environ.pop("GOOGLE_CLIENT_ID", None)
json.dump({"default_leverage": 3.0, "stocks_text": "TCS 2x"}, open(f"{tmp}/funding_rules.json", "w"))
json.dump([{"id": "id1", "symbol": "TCS", "source": "user", "as_of": "2026-10-09"}], open(f"{tmp}/suggestions_log.json", "w"))
import db, funding, import_legacy
import_legacy.main("owner@example.com"); import_legacy.main("owner@example.com")      # twice: harmless
with db.SessionLocal() as s: uid = s.query(db.User).filter_by(email="owner@example.com").one().id
assert funding.load_rules(uid)["default_leverage"] == 3.0
assert funding.load_rules(uid + 1)["customised"] is False                              # nobody else inherits it
assert "watchlist_entries" not in db.Base.metadata.tables                              # the evaluation list is not imported: it is opened from a file in the browser
print("import test passed")
