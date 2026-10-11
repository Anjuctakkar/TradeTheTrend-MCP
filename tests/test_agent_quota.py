import os, tempfile, asyncio, threading, time
tmp = tempfile.mkdtemp()
os.environ.update(DASHBOARD_DATA_DIR=tmp, GOOGLE_CLIENT_ID="test-client.apps.googleusercontent.com", DASHBOARD_SECRET="x"*40,
                  ADMIN_EMAILS="boss@example.com", ALLOWED_EMAILS="friend@example.com,third@example.com", ANTHROPIC_API_KEY="sk-ant-test")
from fastapi import FastAPI
from fastapi.testclient import TestClient
import auth, server, agent_routes, db, usage

class Blk:
    def __init__(self, **kw): self.__dict__.update(kw)
class FakeMessages:
    def __init__(self): self.tools_seen, self.delay, self.mode = [], 0, "plain"
    async def create(self, **kw):
        self.tools_seen.append([t["name"] for t in kw["tools"]])
        if self.delay: await asyncio.sleep(self.delay)
        last = kw["messages"][-1]; u = Blk(input_tokens=100, output_tokens=20)
        if isinstance(last["content"], list):
            return Blk(stop_reason="end_turn", content=[Blk(type="text", text="RESULT:" + last["content"][0]["content"])], usage=u)
        if self.mode == "watchlist":
            return Blk(stop_reason="tool_use", content=[Blk(type="tool_use", id="t1", name="my_watchlist", input={})], usage=u)
        return Blk(stop_reason="end_turn", content=[Blk(type="text", text="hi")], usage=u)
fake = Blk(messages=FakeMessages()); agent_routes._client = fake

def client(email):
    c = TestClient(server.app, follow_redirects=False)
    auth._verify_google = lambda cred: {"email": email, "email_verified": True, "sub": "sub-" + email, "name": email, "picture": None}
    assert c.post("/api/auth/google", json={"credential": "ok"}).status_code == 200
    return c
ask = lambda c, text="hello": c.post("/api/agent/chat", json={"messages": [{"role": "user", "content": text}]})

A, B, T = client("boss@example.com"), client("friend@example.com"), client("third@example.com")

# --- normal call: reply, quota, token log
r = ask(A); assert r.status_code == 200 and r.json()["reply"] == "hi" and r.json()["quota"] == {"used": 1, "limit": agent_routes.USER_DAILY}, r.text
assert A.get("/api/agent/quota").json()["used"] == 1 and B.get("/api/agent/quota").json()["used"] == 0
with db.SessionLocal() as s:
    row = s.query(db.ChatCall).one(); assert (row.input_tokens, row.output_tokens) == (100, 20)

# --- per-user 10-minute limit: A hits it, B is unaffected
agent_routes.USER_RATE = 3
assert ask(A).status_code == 200 and ask(A).status_code == 200
r = ask(A); assert r.status_code == 429 and "wait a few minutes" in r.json()["detail"], r.text
assert ask(B).status_code == 200
agent_routes.USER_RATE = 15

# --- per-user daily limit
agent_routes.USER_DAILY = 2
assert ask(T).status_code == 200 and ask(T).status_code == 200
r = ask(T); assert r.status_code == 429 and "today's 2 questions" in r.json()["detail"], r.text
assert ask(B).status_code == 200   # B has used 1 so far, so this is its 2nd allowed question
assert ask(B).status_code == 429       # the daily limit applies to every user, B included
agent_routes.USER_DAILY = 100

# --- all-users daily limit (counted in the DB, so shared across instances)
with db.SessionLocal() as s: total = s.query(db.ChatCall).count()
agent_routes.DAILY_CAP = total
r = ask(B); assert r.status_code == 429 and "daily limit" in r.json()["detail"], r.text
agent_routes.DAILY_CAP = 500

# --- one user can't run too many at once (the other user can)
agent_routes.MAX_INFLIGHT = 1; fake.messages.delay = 0.6
codes = {}
def run(name, c): codes[name] = ask(c).status_code
t1 = threading.Thread(target=run, args=("a1", A)); t1.start(); time.sleep(0.2)
t2 = threading.Thread(target=run, args=("a2", A)); t3 = threading.Thread(target=run, args=("b1", B)); t2.start(); t3.start()
for t in (t1, t2, t3): t.join()
assert codes == {"a1": 200, "a2": 429, "b1": 200}, codes
assert not agent_routes._inflight, agent_routes._inflight
agent_routes.MAX_INFLIGHT = 2; fake.messages.delay = 0

# --- the assistant sees only the caller's own watchlist
item = {"symbol": "TCS", "as_of": "2026-10-09", "entry": 100.0, "stop_loss": 90.0, "target": 120.0, "score": "4/4", "hold_days": 10}
assert A.post("/api/track/add", json={"source": "user", "items": [item]}).status_code == 200
fake.messages.mode = "watchlist"
ra, rb = ask(A, "show my list").json()["reply"], ask(B, "show my list").json()["reply"]
assert "TCS" in ra and "TCS" not in rb and '"count": 0' in rb, (ra, rb)
assert "my_watchlist" in fake.messages.tools_seen[-1]
fake.messages.mode = "plain"

# --- public service (no sign-in): per-address limits, and no watchlist tool offered
pub = FastAPI(); pub.include_router(agent_routes.build_router(False))
P = TestClient(pub); fake.messages.tools_seen.clear()
r = P.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "hi"}]}); assert r.status_code == 200 and "quota" not in r.json()
assert "my_watchlist" not in fake.messages.tools_seen[-1]

# --- admin cost report
r = A.get("/api/admin/usage"); assert r.status_code == 200
emails = {u["email"]: u for u in r.json()["users"]}; assert emails["boss@example.com"]["input_tokens_7d"] > 0 and "friend@example.com" in emails
assert B.get("/api/admin/usage").status_code == 403
assert TestClient(server.app).get("/api/agent/quota").status_code == 401
print("agent quota tests passed")
