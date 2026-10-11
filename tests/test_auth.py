import os, tempfile, importlib, sys
tmp = tempfile.mkdtemp()
os.environ.update(DASHBOARD_DATA_DIR=tmp, GOOGLE_CLIENT_ID="test-client.apps.googleusercontent.com",
    DASHBOARD_SECRET="x"*40, ADMIN_EMAILS="boss@example.com", ALLOWED_EMAILS="friend@example.com", ALLOWED_DOMAINS="corp.com")
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
import auth, db

app = FastAPI()
auth.install(app)
@app.get("/api/data")
def data(u=Depends(auth.current_user)): return {"uid": u["id"], "role": u["role"]}
@app.get("/api/admin")
def admin(u=Depends(auth.require_admin)): return {"ok": True}
@app.get("/health")
def health(): return {"ok": True}
@app.get("/")
def root(): return {"page": "home"}

def fake(email, verified=True, sub=None):
    def f(cred): 
        if cred == "bad": raise ValueError("bad token")
        return {"email": email, "email_verified": verified, "sub": sub or "sub-"+email, "name": email.split("@")[0], "picture": None}
    return f

def login(client, email, **kw):
    auth._verify_google = fake(email, **kw)
    return client.post("/api/auth/google", json={"credential": "ok"})

c = TestClient(app, follow_redirects=False)
assert c.get("/health").status_code == 200
r = c.get("/"); assert r.status_code == 303 and r.headers["location"].startswith("/login"), r
assert c.get("/api/data").status_code == 401
r = c.get("/login"); assert r.status_code == 200 and "accounts.google.com/gsi/client" in r.text and "test-client" in r.text
assert "accounts.google.com/gsi/client" in r.headers["content-security-policy"]
# bad token
auth._verify_google = fake("x@y.com"); assert c.post("/api/auth/google", json={"credential":"bad"}).status_code == 401
# uninvited / unverified
assert login(c, "stranger@gmail.com").status_code == 403
assert login(c, "friend@example.com", verified=False).status_code == 403
assert c.get("/api/data").status_code == 401
# invited user
r = login(c, "friend@example.com"); assert r.status_code == 200, r.text
assert c.get("/api/data").json()["role"] == "user"
assert c.get("/api/admin").status_code == 403
assert c.get("/api/me").json()["email"] == "friend@example.com"
assert c.get("/").json() == {"page": "home"}
# domain rule + admin
c2 = TestClient(app, follow_redirects=False)
assert login(c2, "anyone@corp.com").status_code == 200
c3 = TestClient(app, follow_redirects=False)
assert login(c3, "boss@example.com").status_code == 200 and c3.get("/api/admin").status_code == 200
# separate users, separate ids
ids = {x.get("/api/data").json()["uid"] for x in (c, c2, c3)}; assert len(ids) == 3
# tampered cookie
tok = c.cookies.get("nse_session"); bad = TestClient(app, follow_redirects=False)
bad.cookies.set("nse_session", tok[:-3] + "abc"); assert bad.get("/api/data").status_code == 401
# cross-site POST refused
assert c.post("/api/logout", headers={"origin": "https://evil.example"}).status_code == 403
# bump session_version -> signed out
with db.SessionLocal() as s:
    u = s.query(db.User).filter_by(email="friend@example.com").one(); u.session_version += 1; s.commit()
assert c.get("/api/data").status_code == 401
# deactivate -> can't sign back in, existing session dead
with db.SessionLocal() as s:
    u = s.query(db.User).filter_by(email="anyone@corp.com").one(); u.is_active = False; s.commit()
assert c2.get("/api/data").status_code == 401
assert login(c2, "anyone@corp.com").status_code == 403
# logout
assert c3.post("/api/logout").status_code == 200
# status endpoint
assert TestClient(app).get("/api/auth/status").json() == {"auth_enabled": True, "user": None}
# rate limit
auth._tries.clear()
codes = [TestClient(app).post("/api/auth/google", json={"credential":"bad"}).status_code for _ in range(22)]
assert 429 in codes, codes
print("signed-in tests passed")
