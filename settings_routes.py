"""
Settings for the LOCAL dashboard: the Anthropic API key and model used by the chat assistant.

Mounted only by server.py (never by the public agent_app.py). Requests must come from the
machine running the dashboard. The key is saved to the .env file next to server.py, applied
immediately, and never sent back to the browser in full (only a masked hint).
"""
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import agent_routes
import auth
import funding
import nse_cache
import usage
import user_store

router = APIRouter()
LOCAL_HOSTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}
KEY_VAR, MODEL_VAR = "ANTHROPIC_API_KEY", "AGENT_MODEL"


def _require_local(request: Request) -> None:
    """API key and model are global, so only an admin (or, with no sign-in configured, this computer) may change them."""
    if auth.AUTH_ENABLED:
        user = getattr(request.state, "user", None)
        if not user or user["role"] != "admin":
            raise HTTPException(403, "Only an administrator can change the API key and model.")
        return
    host = request.client.host if request.client else ""
    if host not in LOCAL_HOSTS:
        raise HTTPException(403, "Settings can only be changed from the computer running the dashboard.")


def _hint(key: str) -> str:
    return f"{key[:7]}…{key[-4:]}" if len(key) > 14 else "set"


def _write_env(updates: dict[str, Optional[str]]) -> None:
    """Set (str) or remove (None) variables in .env, keeping every other line as it was."""
    path = agent_routes.ENV_PATH
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    done: set[str] = set()
    out: list[str] = []
    for line in lines:
        name = line.strip().removeprefix("export ").split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if name in updates:
            if name not in done and updates[name] is not None:
                out.append(f"{name}={updates[name]}")
            done.add(name)
            continue
        out.append(line)
    for name, value in updates.items():
        if name not in done and value is not None:
            out.append(f"{name}={value}")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(nl.join(out) + nl, encoding="utf-8")
    tmp.replace(path)


async def _test() -> dict:
    key, model = os.getenv(KEY_VAR), agent_routes.get_model()
    if not key:
        return {"ok": False, "code": "no_key", "message": "No Anthropic API key is set."}
    try:
        from anthropic import AsyncAnthropic
        await AsyncAnthropic(api_key=key).messages.create(
            model=model, max_tokens=8, messages=[{"role": "user", "content": "Say hi"}])
        return {"ok": True, "message": f"Works. The key is valid and {model} responded."}
    except Exception as exc:  # noqa: BLE001
        print(f"[settings] test failed: {type(exc).__name__}: {exc}", flush=True)
        _, message, code = agent_routes.classify_error(exc, model, detailed=True)
        return {"ok": False, "code": code, "message": message}


class SettingsUpdate(BaseModel):
    api_key: Optional[str] = None   # None = leave the key as it is
    model: Optional[str] = None     # None = leave; "" = back to the default


def _status() -> dict:
    key = os.getenv(KEY_VAR) or ""
    return {"has_key": bool(key), "key_hint": _hint(key) if key else None, "model": agent_routes.get_model(),
            "default_model": agent_routes.DEFAULT_MODEL, "env_file": str(agent_routes.ENV_PATH)}


@router.get("/api/settings")
def get_settings(request: Request):
    _require_local(request)
    return _status()


@router.post("/api/settings")
async def save_settings(req: SettingsUpdate, request: Request):
    """Save the key and/or model, then test the result."""
    _require_local(request)
    updates: dict[str, Optional[str]] = {}
    if req.api_key is not None:
        key = req.api_key.strip()
        if not key.startswith("sk-ant-") or len(key) < 20 or any(c.isspace() or c in "\"'" for c in key):
            raise HTTPException(400, "That doesn't look like an Anthropic API key. It should start with sk-ant- and have no spaces or quotes.")
        updates[KEY_VAR] = key
    if req.model is not None:
        model = req.model.strip()
        if model and not all(c.isalnum() or c in "-._:" for c in model):
            raise HTTPException(400, "The model name has characters that aren't allowed.")
        updates[MODEL_VAR] = model or None
    if not updates:
        raise HTTPException(400, "Nothing to save.")
    _write_env(updates)
    for name, value in updates.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    agent_routes.reset_client()
    return {**_status(), "test": await _test()}


@router.post("/api/settings/test")
async def test_settings(request: Request):
    _require_local(request)
    return await _test()


@router.delete("/api/settings/key")
def remove_key(request: Request):
    _require_local(request)
    _write_env({KEY_VAR: None})
    os.environ.pop(KEY_VAR, None)
    agent_routes.reset_client()
    return _status()


# ---------------------------------------------------------------- per-user funding rules and preferences
# Every signed-in user has their own rules and preferences, stored server-side so they follow the
# user across devices. A new user starts on the defaults.
def _rules_payload(user_id: int) -> dict:
    rules = funding.load_rules(user_id)
    return {"rules": {k: v for k, v in rules.items() if k != "stocks"}, "stocks_text": funding.stocks_to_text(rules["stocks"]),
            "defaults": funding.DEFAULT_RULES}


@router.get("/api/funding/rules")
def get_funding_rules(user: dict = Depends(auth.current_user)):
    return _rules_payload(user["id"])


@router.post("/api/funding/rules")
def save_funding_rules(payload: dict[str, Any], user: dict = Depends(auth.current_user)):
    """Validate and save this user's funding rules. Nothing is saved if any value is invalid."""
    rules, errors = funding.validate(payload)
    if errors:
        raise HTTPException(400, " ".join(errors[:6]))
    funding.save_rules(rules, user["id"])
    return _rules_payload(user["id"])


@router.delete("/api/funding/rules")
def reset_funding_rules(user: dict = Depends(auth.current_user)):
    """Back to the built-in defaults for this user only."""
    funding.reset_rules(user["id"])
    return _rules_payload(user["id"])


class PrefsUpdate(BaseModel):
    live_symbols: Optional[str] = None      # the Live tab's watch list


@router.get("/api/prefs")
def get_prefs(user: dict = Depends(auth.current_user)):
    return user_store.get_prefs(user["id"])


@router.put("/api/prefs")
def put_prefs(body: PrefsUpdate, user: dict = Depends(auth.current_user)):
    updates: dict[str, Any] = {}
    if body.live_symbols is not None:
        updates["live_symbols"] = body.live_symbols[:400]
    return user_store.set_prefs(user["id"], updates) if updates else user_store.get_prefs(user["id"])


@router.get("/api/admin/usage")
def admin_usage(days: int = 7, user: dict = Depends(auth.require_admin)):
    """Questions and tokens per user over the last `days` days (admin only), to watch cost."""
    return {"days": max(1, min(days, 30)), "users": usage.report(max(1, min(days, 30)))}


@router.get("/api/admin/cache")
def admin_cache(user: dict = Depends(auth.require_admin)):
    """How well the shared NSE cache is working (admin only)."""
    return nse_cache.CACHE.snapshot()


@router.delete("/api/admin/cache")
def admin_cache_clear(tool: str = "", user: dict = Depends(auth.require_admin)):
    """Drop cached NSE data, all of it or one tool's (admin only). It refills on demand."""
    return {"removed": nse_cache.CACHE.clear(tool or None)}
