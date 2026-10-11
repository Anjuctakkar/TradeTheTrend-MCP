"""
Chat endpoint for the agent, as a FastAPI router. Used two ways:
  * mounted inside the local dashboard (server.py) with detailed=True, so errors
    such as "credit balance too low" are shown in the chat with what to do; and
  * by agent_app.py, the public-facing service, with detailed=False: visitors
    only see generic messages and the details stay in the server log.

agent_tools (which imports server.py) is imported lazily, at request time,
so server.py can include this router without a circular import.
"""
import asyncio
import collections
import datetime as dt
import os
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

import config
import usage

ENV_PATH = config.DATA_DIR / ".env"
# utf-8-sig: Windows Notepad can save a BOM that would otherwise hide the first key.
# override=True: a key saved in .env (e.g. from the dashboard's Settings page) wins over an old OS variable.
load_dotenv(ENV_PATH, encoding="utf-8-sig", override=True)

from fastapi import APIRouter, HTTPException, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import agent  # noqa: E402

DEFAULT_MODEL = "claude-sonnet-5-5"
RATE = int(os.getenv("AGENT_RATE_PER_10MIN", "20"))
DAILY_CAP = int(os.getenv("AGENT_DAILY_CAP", "500"))                      # all users together, counted in the database
USER_RATE = int(os.getenv("AGENT_USER_RATE_PER_10MIN", "15"))              # per signed-in user
USER_DAILY = int(os.getenv("AGENT_USER_DAILY_CAP", "100"))                 # per signed-in user, resets at midnight IST
MAX_INFLIGHT = int(os.getenv("AGENT_USER_MAX_INFLIGHT", "2"))              # questions one user may have running at once (per process)
MAX_MESSAGES, MAX_CHARS = 20, 2000

# Problems the user can fix themselves in Settings; the chat offers an "Open settings" button for these.
SETUP_CODES = {"no_key", "invalid_key", "billing", "model", "permission"}

_client = None
_hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)
_daily = {"day": dt.date.today(), "n": 0}
_inflight: collections.Counter = collections.Counter()


def get_model() -> str:
    return os.getenv("AGENT_MODEL") or DEFAULT_MODEL


def reset_client() -> None:
    """Drop the cached Anthropic client so the next request picks up a new key."""
    global _client
    _client = None


def classify_error(exc: BaseException, model: str, detailed: bool) -> tuple[int, str, str]:
    """Turn an exception from the Claude call into (http_status, message, code)."""
    msg = str(getattr(exc, "message", "") or exc)
    kind = "error"
    try:
        import anthropic
        if "credit balance" in msg.lower():
            kind = "billing"
        elif isinstance(exc, anthropic.AuthenticationError):
            kind = "invalid_key"
        elif isinstance(exc, anthropic.PermissionDeniedError):
            kind = "permission"
        elif isinstance(exc, anthropic.NotFoundError):
            kind = "model"
        elif isinstance(exc, anthropic.RateLimitError):
            kind = "rate_limit"
        elif isinstance(exc, anthropic.APIConnectionError):
            kind = "connection"
    except ImportError:
        pass

    if detailed:
        table = {
            "billing": (402, "Your Anthropic credit balance is too low. Add credit under Plans & Billing in the Anthropic Console, then try again."),
            "invalid_key": (401, "Anthropic rejected the API key. Check it, or replace it in Settings."),
            "permission": (403, "This API key isn't allowed to use the selected model. Check the key's workspace, or choose another model in Settings."),
            "model": (404, f"The model '{model}' isn't available to this key. Choose another model in Settings."),
            "rate_limit": (429, "Anthropic is rate-limiting this key. Wait a minute and try again."),
            "connection": (502, "Couldn't reach the Anthropic API. Check your internet connection or proxy."),
            "error": (502, f"The assistant hit a problem ({type(exc).__name__}). Details are in the server window."),
        }
    else:
        table = {
            "rate_limit": (429, "The assistant is busy. Please try again in a minute."),
            "error": (502, "The assistant hit a problem. Please try again."),
        }
        for k in ("billing", "invalid_key", "permission", "model", "connection"):
            table[k] = (503, "The assistant is temporarily unavailable. Please try again later.")
    status, message = table[kind]
    return status, message, kind


class Msg(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[Msg]


def _client_ip(req: Request) -> str:
    return config.client_ip(req)


def _check_limits(ip: str) -> None:
    today = dt.date.today()
    if _daily["day"] != today:
        _daily.update(day=today, n=0)
    if _daily["n"] >= DAILY_CAP:
        raise HTTPException(429, "The assistant has reached its daily limit. Please try again tomorrow.")
    q, now = _hits[ip], time.time()
    while q and now - q[0] > 600:
        q.popleft()
    if len(q) >= RATE:
        raise HTTPException(429, "Too many requests. Please wait a few minutes.")
    q.append(now)
    _daily["n"] += 1


def build_router(detailed: bool) -> APIRouter:
    r = APIRouter()

    @r.post("/api/agent/chat")
    async def chat(req: ChatRequest, request: Request):
        global _client
        msgs = req.messages[-MAX_MESSAGES:]
        if not msgs or msgs[0].role != "user" or msgs[-1].role != "user":
            raise HTTPException(400, "Conversation must start and end with a user message.")
        if any(m.role not in ("user", "assistant") for m in msgs) or any(a.role == b.role for a, b in zip(msgs, msgs[1:])):
            raise HTTPException(400, "Messages must alternate between user and assistant.")
        if any(not m.content.strip() or len(m.content) > MAX_CHARS for m in msgs):
            raise HTTPException(400, f"Each message must be 1 to {MAX_CHARS} characters.")
        if not os.getenv("ANTHROPIC_API_KEY"):
            print(f"[agent] ANTHROPIC_API_KEY not found. Looked for {ENV_PATH} (file exists: {ENV_PATH.exists()}). "
                  "Restart the server after editing .env.", flush=True)
            if detailed:
                return JSONResponse({"detail": "No Anthropic API key is set. Add one in Settings.", "code": "no_key"}, status_code=503)
            raise HTTPException(503, "The assistant isn't configured yet.")

        # Signed-in users (the dashboard) are limited per user, in the database, so limits hold across
        # instances and restarts. The public agent service has no sign-in and keeps the per-address limits.
        user = getattr(request.state, "user", None)
        uid = user["id"] if user else None
        call_id = None
        if uid is not None:
            if _inflight[uid] >= MAX_INFLIGHT:
                raise HTTPException(429, "You already have questions running. Wait for them to finish, then ask again.")
            try:
                call_id = await asyncio.to_thread(usage.start_call, uid, USER_RATE, USER_DAILY, DAILY_CAP)
            except usage.LimitReached as lim:
                raise HTTPException(429, str(lim))
            _inflight[uid] += 1
        else:
            _check_limits(_client_ip(request))

        model = get_model()
        tokens = {"input": 0, "output": 0}
        try:
            import agent_tools  # lazy: avoids a circular import with server.py
            if _client is None:
                from anthropic import AsyncAnthropic
                _client = AsyncAnthropic()
            handlers = agent_tools.handlers_for(uid) if uid is not None else agent_tools.HANDLERS
            tools = agent.TOOLS + [agent.WATCHLIST_TOOL] if uid is not None else agent.TOOLS
            reply, used, tokens = await agent.run_agent(
                _client, model, [{"role": m.role, "content": m.content} for m in msgs], handlers, tools=tools)
        except Exception as exc:  # noqa: BLE001 - classify known problems, never leak internals publicly
            print(f"[agent] error: {type(exc).__name__}: {exc}", flush=True)
            status, message, code = classify_error(exc, model, detailed)
            return JSONResponse({"detail": message, "code": code}, status_code=status)
        finally:
            if uid is not None:
                _inflight[uid] -= 1
                if _inflight[uid] <= 0:
                    del _inflight[uid]
                await asyncio.to_thread(usage.finish_call, call_id, tokens)
        out = {"reply": reply, "tools_used": used}
        if uid is not None:
            out["quota"] = {"used": await asyncio.to_thread(usage.used_today, uid), "limit": USER_DAILY}
        return out

    @r.get("/api/agent/quota")
    async def quota(request: Request):
        user = getattr(request.state, "user", None)
        if not user:
            return {"limit": None}
        return {"used": await asyncio.to_thread(usage.used_today, user["id"]), "limit": USER_DAILY}

    return r


router = build_router(detailed=False)  # public default; server.py mounts build_router(True)
