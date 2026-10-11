import os, tempfile, asyncio
tmp = tempfile.mkdtemp()
os.environ.update(DASHBOARD_DATA_DIR=tmp, GOOGLE_CLIENT_ID="test-client.apps.googleusercontent.com", DASHBOARD_SECRET="x"*40,
                  ADMIN_EMAILS="boss@example.com", ALLOWED_EMAILS="friend@example.com,new@example.com")
from fastapi.testclient import TestClient
import auth, server, funding

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
item = {"symbol": "TCS", "as_of": "2026-10-09", "entry": 100.0, "stop_loss": 90.0, "target": 120.0, "score": "4/4", "hold_days": 10}
watch = {"symbol": "KOTAKBANK", "as_of": "2026-10-09"}

# --- watchlist: separate histories
r = A.post("/api/track/add", json={"source": "user", "items": [item, watch]}); assert r.status_code == 200 and r.json()["added"] == 2, r.text
a_ids = r.json()["ids"]
assert B.get("/api/track/list").json()["entries"] == []                       # a user with no history sees none
assert B.get("/api/track").json()["entries"] == []
r = B.post("/api/track/add", json={"source": "user", "items": [item]}); assert r.json()["added"] == 1   # same stock+date is NOT "already there" for B
b_id = r.json()["ids"][0]; assert b_id not in a_ids
assert len(A.get("/api/track/list").json()["entries"]) == 2
assert [e["id"] for e in B.get("/api/track/list").json()["entries"]] == [b_id]
# duplicates inside one user's list are still skipped
assert A.post("/api/track/add", json={"source": "user", "items": [item]}).json() == {"added": 0, "skipped": 1, "ids": [a_ids[0]]}
# B can't touch A's rows (looks like "not found")
assert B.post(f"/api/track/{a_ids[0]}/taken", json={"taken": True}).status_code == 404
assert B.delete(f"/api/track/{a_ids[0]}").status_code == 404
a_rows = A.get("/api/track/list").json()["entries"]
assert len(a_rows) == 2 and not any(e["taken"] for e in a_rows)
# owner can
assert A.post(f"/api/track/{a_ids[0]}/taken", json={"taken": True}).status_code == 200
assert B.delete(f"/api/track/{b_id}").status_code == 200
assert len(A.get("/api/track/list").json()["entries"]) == 2 and B.get("/api/track/list").json()["entries"] == []
# outcomes endpoint only evaluates the caller's entries
assert {e["symbol"] for e in A.get("/api/track").json()["entries"]} == {"TCS", "KOTAKBANK"}

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
for path in ("/api/track/list", "/api/funding/rules", "/api/prefs", "/api/tools?server=bhavcopy"):
    assert anon.get(path).status_code == 401, path
print("isolation tests passed")
