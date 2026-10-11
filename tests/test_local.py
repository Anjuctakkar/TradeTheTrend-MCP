import os, tempfile
os.environ.update(DASHBOARD_DATA_DIR=tempfile.mkdtemp()); os.environ.pop("GOOGLE_CLIENT_ID", None)
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
import auth
app = FastAPI(); auth.install(app)
@app.get("/api/admin")
def a(u=Depends(auth.require_admin)): return u
c = TestClient(app)
assert c.get("/api/admin").json()["email"] == "local@localhost"
assert c.get("/login", follow_redirects=False).status_code == 303
assert c.get("/api/auth/status").json()["auth_enabled"] is False
print("local-mode tests passed")
