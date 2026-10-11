"""
Public-facing agent service: ONLY the chat endpoint, the widget script and a
health check. (The dashboard in server.py has open tool-calling endpoints;
keep it local.)

Run:  python -m uvicorn agent_app:app --host 0.0.0.0 --port 8001
Env:  ANTHROPIC_API_KEY        required
      AGENT_ALLOWED_ORIGINS    comma-separated site origins, e.g. https://www.example.com
      AGENT_MODEL              default claude-sonnet-5-5
      AGENT_RATE_PER_10MIN     per-IP requests per 10 min (default 20)
      AGENT_DAILY_CAP          total requests per day, a cost ceiling (default 500)
      AGENT_DEMO               1 = serve a test page at / (local testing only)
"""
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

import agent_routes

ORIGINS = [o.strip() for o in os.getenv("AGENT_ALLOWED_ORIGINS", "").split(",") if o.strip()]

app = FastAPI(title="NSE agent")
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["POST", "GET"], allow_headers=["Content-Type"])
app.include_router(agent_routes.router)

DEMO = os.getenv("AGENT_DEMO", "").lower() in ("1", "true", "yes")
if DEMO:
    # Local test page: same origin as the API, so no CORS setup needed. Leave off in production.
    @app.get("/", response_class=HTMLResponse)
    def demo():
        return """<!doctype html><meta charset="utf-8"><title>Agent demo</title>
<body style="font-family:system-ui;max-width:640px;margin:60px auto;padding:0 20px">
<h1>NSE assistant demo</h1><p>Click the chat bubble in the bottom-right corner.</p>
<script src="/agent-widget.js" defer></script></body>"""


@app.get("/health")
def health():
    return {"ok": True, "model": agent_routes.get_model(), "origins_configured": bool(ORIGINS)}


@app.get("/agent-widget.js")
def widget():
    return FileResponse(Path(__file__).resolve().parent / "static" / "agent-widget.js",
                        media_type="application/javascript", headers={"Cache-Control": "public, max-age=300"})
