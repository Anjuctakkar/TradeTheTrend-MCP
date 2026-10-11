import os, tempfile, asyncio
tmp = tempfile.mkdtemp()
os.environ.update(DASHBOARD_DATA_DIR=tmp, GOOGLE_CLIENT_ID="test-client.apps.googleusercontent.com", DASHBOARD_SECRET="x"*40,
                  ADMIN_EMAILS="boss@example.com", ALLOWED_EMAILS="friend@example.com,new@example.com")
from fastapi.testclient import TestClient
import auth, db, server, funding

def fake(email):
    return lambda cred: {"email": email, "email_verified": True, "sub": "sub-" + email, "name": email.split("@")[0], "picture": None}
def client(email):
    c = TestClient(server.app, follow_redirects=False)
    auth._verify_google = fake(email)
    assert c.post("/api/auth/google", json={"credential": "ok"}).status_code == 200
    return c

async def no_hist(*a, **k): return []
async def no_tools(url): return []
server._fetch_history_adjusted = no_hist
server._list_tools = no_tools

A, B = client("boss@example.com"), client("friend@example.com")

# --- the evaluation list is NOT kept on the server: it lives on each user's own computer
assert "watchlist_entries" not in db.Base.metadata.tables
for path in ("/api/track/list", "/api/track"): assert A.get(path).status_code == 404, path
assert A.post("/api/track/add", json={"source": "user", "items": []}).status_code in (404, 405)

# --- ...but the server can still check outcomes for the list a browser sends (stateless)
async def hist(url, sym, months, partial_ok=False):
    return [{"date": f"2026-10-{i + 1:02d}", "open": 100 + 2 * i, "high": 100 + 2 * i, "low": 100 + 2 * i, "close": 100 + 2 * i} for i in range(20)]
server._fetch_history_adjusted = hist
e1 = {"id": "abc12345", "symbol": "tcs", "as_of": "2026-10-05", "entry": 108.0, "stop_loss": 100.0, "target": 124.0, "source": "user", "hold_days": 20, "taken": False}
r = A.post("/api/track/evaluate", json={"entries": [e1]}); assert r.status_code == 200, r.text
row = r.json()["entries"][0]; assert row["status"] == "target" and row["id"] == "abc12345" and row["symbol"] == "TCS", row
assert B.post("/api/track/evaluate", json={"entries": [{**e1, "id": "zzz99999", "stop_loss": 120.0, "entry": 125.0, "target": 130.0}]}).json()["entries"][0]["status"] in ("stopped", "open", "pending", "expired", "target")
assert A.post("/api/track/evaluate", json={"entries": []}).json() == {"entries": []}
async def no_hist(*a, **k): return []
server._fetch_history_adjusted = no_hist
assert A.post("/api/track/evaluate", json={"entries": [e1]}).json()["entries"][0]["status"] == "no_data"
# bad input is refused
for bad in ({**e1, "stop_loss": 110.0}, {**e1, "symbol": "bad symbol!"}, {**e1, "as_of": "yesterday"}):
    assert A.post("/api/track/evaluate", json={"entries": [bad]}).status_code == 400, bad
assert A.post("/api/track/evaluate", json={"entries": [{**e1, "id": str(i)} for i in range(301)]}).status_code == 400
assert A.post("/api/track/evaluate", json={"entries": [{"symbol": "TCS"}]}).status_code == 422
assert TestClient(server.app).post("/api/track/evaluate", json={"entries": [e1]}).status_code == 401

# --- funding rules: separate settings, new users start on defaults
assert A.get("/api/funding/rules").json()["rules"]["customised"] is False
r = A.post("/api/funding/rules", json={"default_leverage": 3, "stocks_text": "TCS 2x"}); assert r.status_code == 200 and r.json()["rules"]["default_leverage"] == 3
d = B.get("/api/funding/rules").json(); assert d["rules"]["default_leverage"] == 2.7 and d["rules"]["customised"] is False and d["stocks_text"] == ""
assert A.get("/api/funding/rules").json()["rules"]["customised"] is True
N = client("new@example.com"); assert N.get("/api/funding/rules").json()["rules"]["default_leverage"] == 2.7   # brand-new user
uid = lambda c: c.get("/api/me").json()["id"]
assert funding.load_rules(uid(A))["default_leverage"] == 3 and funding.load_rules(uid(B))["default_leverage"] == 2.7
assert B.post("/api/funding/rules", json={"default_leverage": 9}).status_code == 400                          # validation still applies
assert B.delete("/api/funding/rules").status_code == 200
assert A.get("/api/funding/rules").json()["rules"]["default_leverage"] == 3                                   # B's reset didn't touch A
assert len(funding.parse_stock_list("\n".join(f"S{i} 2x" for i in range(500)))[0]) == 200                     # input size is capped

# --- preferences
assert A.put("/api/prefs", json={"live_symbols": "TCS, INFY"}).json() == {"live_symbols": "TCS, INFY"}
assert B.get("/api/prefs").json() == {} and A.get("/api/prefs").json()["live_symbols"] == "TCS, INFY"

# --- admin-only and signed-out access
assert B.get("/api/tools?server=bhavcopy").status_code == 403 and A.get("/api/tools?server=bhavcopy").status_code == 200
assert B.post("/api/call", json={"server": "bhavcopy", "tool": "x"}).status_code == 403
assert B.get("/api/settings").status_code == 403 and A.get("/api/settings").status_code == 200
assert B.post("/api/settings/test").status_code == 403
anon = TestClient(server.app, follow_redirects=False)
for path in ("/api/funding/rules", "/api/prefs", "/api/tools?server=bhavcopy"):
    assert anon.get(path).status_code == 401, path
print("isolation tests passed")
