"""
NSE MCP Dashboard - local FastAPI app over two NSE MCP servers.

Changes in this version:
  * CM server URL aligned with the .bat/watcher (mcp.nseindia.in); both URLs
    can be overridden with NSE_BHAV_URL / NSE_CM_URL env vars.
  * One MCP session per server is cached and reused (re-handshake on expiry).
  * Stocks are analysed concurrently (bounded).
  * Splits/bonuses inside the history window are applied to earlier closes
    via get_corporate_actions, so SMA/RSI/MACD and signals aren't distorted.
  * /api/recommend is wired to recommend.py.

Run:  pip install -r requirements.txt && python server.py   (Python 3.11+)
"""

import asyncio
import contextlib
import datetime as dt
import itertools
import json
import time
import os
import re
from pathlib import Path
from typing import Any, Optional

import httpx
import uvicorn
from sqlalchemy import text

try:
    from dotenv import load_dotenv

    load_dotenv(Path(os.getenv("DASHBOARD_DATA_DIR") or Path(__file__).resolve().parent) / ".env", encoding="utf-8-sig", override=True)
except ImportError:  # .env loading is optional for the dashboard
    pass
from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import allocation
import auth
import backtest
import db
import funding
import holding
import nse_cache
import recommend
import sectors
import sr_model
import technical

app = FastAPI(title="NSE MCP Dashboard",
              docs_url=None if auth.PUBLIC else "/docs", redoc_url=None if auth.PUBLIC else "/redoc",
              openapi_url=None if auth.PUBLIC else "/openapi.json")
auth.install(app)   # login + security headers; a no-op for local use unless DASHBOARD_PASSWORD_HASH is set


@app.get("/health")
def health():
    """For the host's health check: the app is up and the database answers."""
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        print(f"[health] database check failed: {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(503, "database unavailable")
    return {"ok": True, "build": "sr-2026-10-16.11"}

SERVERS: dict[str, str] = {
    "bhavcopy": os.getenv("NSE_BHAV_URL", "https://mcp.nseindia.in/bhavcopy/cm/mcp"),
    "cm": os.getenv("NSE_CM_URL", "https://mcp.nseindia.in/cmmkt/mcp"),
}

_ID_COUNTER = itertools.count(1)
_INIT_PARAMS = {
    "protocolVersion": "2025-03-26",  # same as the working watcher/.bat
    "capabilities": {},
    "clientInfo": {"name": "nse-mcp-dashboard", "version": "0.2.0"},
}

ANALYSIS_SERVER = "bhavcopy"
ANALYSIS_HISTORY_MONTHS = 12  # also gives the 52-week range and enough trades to cross-check hold periods
ANALYSIS_CONCURRENCY = 3
# Network behaviour towards the NSE MCP servers (override with environment variables / .env).
NSE_READ_TIMEOUT = float(os.getenv("NSE_READ_TIMEOUT", "60"))   # seconds to wait for a reply
NSE_RETRIES = int(os.getenv("NSE_RETRIES", "1"))                # extra attempts after a timeout or 502/503/504
NSE_MAX_INFLIGHT = int(os.getenv("NSE_MAX_INFLIGHT", "4"))      # simultaneous requests to NSE across the whole app
NSE_ATTEMPTS = max(NSE_RETRIES, 1) + 1                           # always at least one retry (covers an expired session)


class CallToolRequest(BaseModel):
    server: str
    tool: str
    arguments: dict[str, Any] = {}


class StockAllocation(BaseModel):
    symbol: str
    allocation: float = 0


class AnalyzeRequest(BaseModel):
    stocks: list[StockAllocation]
    mode: str = "equal"           # "equal" uses each stock's `allocation`; "suggested" lets the app decide
    total_capital: float = 0
    max_weight_pct: float = 35.0
    use_mtf: bool = False                 # fund part of each position with MTF (rules in funding_rules.json)
    mtf_limit: Optional[float] = None     # broker MTF limit in Rs; None = no limit
    signal_model: str = "trend"           # which setup drives capital split and funding outcomes: "trend" or "sr"


class RecommendRequest(BaseModel):
    budget: float
    existing: list[dict[str, Any]] = []
    model: str = "trend"          # which rule set may produce picks: "trend", "sr" or "either"


def _to_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _server_url(server_key: str) -> str:
    if server_key not in SERVERS:
        raise HTTPException(400, f"Unknown server '{server_key}'. Known: {list(SERVERS)}")
    return SERVERS[server_key]


def _describe_error(exc: BaseException) -> str:
    parts: list[str] = []

    def walk(e: BaseException) -> None:
        if isinstance(e, BaseExceptionGroup):
            for sub in e.exceptions:
                walk(sub)
        elif isinstance(e, httpx.TimeoutException):
            parts.append(f"{type(e).__name__}: NSE did not reply within {NSE_READ_TIMEOUT:.0f}s "
                         f"(tried {NSE_ATTEMPTS} times). NSE's servers are slow or down right now; try again shortly")
        elif isinstance(e, httpx.ConnectError):
            parts.append(f"ConnectError: couldn't connect to NSE ({e}). Check your internet connection or proxy")
        else:
            parts.append(f"{type(e).__name__}: {e}")

    walk(exc)
    return " | ".join(parts) if parts else str(exc)


def _looks_empty(value: Any) -> bool:
    if value in (None, {}, []):
        return True
    return isinstance(value, dict) and set(value.keys()) <= {"type"}


# ---------------------------------------------------------------------------
# MCP transport with a cached session per server URL
# ---------------------------------------------------------------------------
_client: Optional[httpx.AsyncClient] = None
_sessions: dict[str, Optional[str]] = {}
_session_locks: dict[str, asyncio.Lock] = {}


_mcp_sem: Optional[asyncio.Semaphore] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # Short keep-alive: a pooled connection that NSE's gateway has silently dropped would otherwise hang until the timeout.
        _client = httpx.AsyncClient(timeout=httpx.Timeout(NSE_READ_TIMEOUT, connect=15.0),
                                    limits=httpx.Limits(max_connections=10, keepalive_expiry=5.0))
    return _client


def _sem() -> asyncio.Semaphore:
    global _mcp_sem
    if _mcp_sem is None:
        _mcp_sem = asyncio.Semaphore(max(1, NSE_MAX_INFLIGHT))
    return _mcp_sem


async def _post(url: str, method: str, params: dict[str, Any], session_id: Optional[str],
                expect_response: bool = True) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
    if expect_response:
        payload["id"] = next(_ID_COUNTER)

    async with _sem():
        resp = await _get_client().post(url, json=payload, headers=headers)
    resp.raise_for_status()
    new_sid = resp.headers.get("mcp-session-id", session_id)
    if not expect_response:
        return None, new_sid

    data: Optional[dict[str, Any]] = None
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            line = line.strip()
            if line.startswith("data:") and line[5:].strip():
                try:
                    data = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
        if data is None:
            raise RuntimeError(f"Could not parse SSE response for '{method}'")
    else:
        data = resp.json()
    if "error" in data:
        err = data["error"]
        raise RuntimeError(f"MCP error {err.get('code')}: {err.get('message')}")
    return data.get("result"), new_sid


async def _ensure_session(url: str, force: bool = False) -> Optional[str]:
    lock = _session_locks.setdefault(url, asyncio.Lock())
    async with lock:
        if url in _sessions and not force:
            return _sessions[url]
        _, sid = await _post(url, "initialize", _INIT_PARAMS, None)
        await _post(url, "notifications/initialized", {}, sid, expect_response=False)
        _sessions[url] = sid
        return sid


_RETRYABLE = (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError)


async def _rpc(url: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Send a request on the cached session. A timeout, a dropped connection, a 502/503/504, or a
    session the server no longer knows (400/401/404) triggers a fresh handshake and another attempt."""
    attempts = NSE_ATTEMPTS
    for attempt in range(attempts):
        sid = await _ensure_session(url)
        try:
            result, _ = await _post(url, method, params, sid)
            return result or {}
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (400, 401, 404, 502, 503, 504) or attempt == attempts - 1:
                raise
        except _RETRYABLE:
            if attempt == attempts - 1:
                raise
        _sessions.pop(url, None)               # force a new handshake next time
        await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError("unreachable")


async def _list_tools(url: str) -> list[dict[str, Any]]:
    try:
        result = await _rpc(url, "tools/list", {})
        return [{"name": t.get("name", ""), "description": t.get("description") or "",
                 "input_schema": t.get("inputSchema") or {}} for t in result.get("tools", [])]
    except BaseException as exc:  # noqa: BLE001
        raise RuntimeError(_describe_error(exc)) from exc


async def _call_tool_raw(url: str, tool_name: str, arguments: dict[str, Any]) -> Any:
    try:
        result = await _rpc(url, "tools/call", {"name": tool_name, "arguments": arguments})
        is_error = bool(result.get("isError", False))

        structured = result.get("structuredContent")
        if not _looks_empty(structured):
            return {"is_error": is_error, "content": structured}

        blocks: list[Any] = []
        for block in result.get("content", []) or []:
            if not isinstance(block, dict):
                blocks.append(block)
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if text is None:
                    text = block.get("data") or block.get("value") or block.get("content")
                if text is None:
                    blocks.append(block)
                    continue
                try:
                    blocks.append(json.loads(text))
                except (json.JSONDecodeError, TypeError):
                    blocks.append(text)
            else:
                blocks.append(block)

        friendly = blocks if len(blocks) != 1 else blocks[0]
        if _looks_empty(friendly):
            return {"is_error": is_error, "content": result,
                    "note": "No populated 'content'/'structuredContent' - showing the raw MCP result."}
        return {"is_error": is_error, "content": friendly}
    except BaseException as exc:  # noqa: BLE001
        raise RuntimeError(_describe_error(exc)) from exc


async def _call_tool(url: str, tool_name: str, arguments: dict[str, Any], use_cache: bool = True) -> Any:
    """Every NSE call goes through here. Market data is the same for every user, so answers are shared
    through nse_cache (see that file for lifetimes). use_cache=False always asks NSE."""
    if not use_cache:
        return await _call_tool_raw(url, tool_name, arguments)
    return await nse_cache.CACHE.get_or_fetch(tool_name, url, arguments, lambda: _call_tool_raw(url, tool_name, arguments))


# Big scans (Suggest stocks) hit NSE hard. Only NSE_HEAVY_JOBS run at once across all users; the rest queue in order.
NSE_HEAVY_JOBS = int(os.getenv("NSE_HEAVY_JOBS", "2"))
_heavy: Optional[asyncio.Semaphore] = None
_scanning: set[int] = set()


@contextlib.asynccontextmanager
async def heavy_slot():
    global _heavy
    if _heavy is None:
        _heavy = asyncio.Semaphore(max(1, NSE_HEAVY_JOBS))
    async with _heavy:
        yield


@app.on_event("shutdown")
async def _close_client() -> None:
    if _client is not None:
        await _client.aclose()


@app.get("/api/servers")
def get_servers():
    return dict(SERVERS)



# ---------------------------------------------------------------------------
# Live prices: CM market data server (refreshes about every minute while the market is open)
# ---------------------------------------------------------------------------
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))   # India has no daylight saving


def _market_status() -> dict[str, Any]:
    now = dt.datetime.now(IST)
    t = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        state, why = "closed", "weekend"
    elif t < 9 * 60:
        state, why = "closed", "before the pre-open session"
    elif t < 9 * 60 + 15:
        state, why = "pre-open", "pre-open session (9:00 to 9:15 am)"
    elif t <= 15 * 60 + 30:
        state, why = "open", "regular session (9:15 am to 3:30 pm)"
    else:
        state, why = "closed", "after the 3:30 pm close"
    return {"state": state, "why": why, "now_ist": now.strftime("%d %b %Y %H:%M:%S"),
            "note": "Based on weekday and time only; NSE holidays aren't checked, so rely on the data age."}


def _nk(k: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def _pick(d: dict[str, Any], names: list[str]) -> Any:
    m = {_nk(k): v for k, v in d.items()}
    for n in names:
        if m.get(n) not in (None, ""):
            return m[n]
    return None


def _num(v: Any) -> Optional[float]:
    if isinstance(v, str):
        v = v.replace(",", "").replace("%", "").strip()
    return _to_float(v)


def _first_record(c: Any) -> Optional[dict[str, Any]]:
    """The quote dict inside whatever wrapper the tool used (for example {"updatedAt": ..., "stock": {...}})."""
    if isinstance(c, list):
        return _first_record(c[0]) if c else None
    if isinstance(c, dict):
        for k in ("data", "quote", "result", "quotes", "stock"):
            if k in c and isinstance(c[k], (dict, list)):
                inner = _first_record(c[k])
                if inner:
                    return inner
        # No known wrapper name: if there is no price here but exactly one nested object, the quote is inside it.
        nested = [v for v in c.values() if isinstance(v, dict)]
        if len(nested) == 1 and not _pick(c, ["ltp", "lastprice", "lasttradedprice"]):
            return _first_record(nested[0]) or c
        return c
    return None


def _parse_ts(v: Any) -> Optional[dt.datetime]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        secs = v / 1000 if v > 1e11 else v
        return dt.datetime.fromtimestamp(secs, IST)
    txt = str(v).strip().replace("Z", "+00:00")
    for parse in (dt.datetime.fromisoformat,
                  lambda x: dt.datetime.strptime(x, "%d-%b-%Y %H:%M:%S"),
                  lambda x: dt.datetime.strptime(x, "%d-%m-%Y %H:%M:%S"),
                  lambda x: dt.datetime.strptime(x, "%d/%m/%Y %H:%M:%S")):
        try:
            d = parse(txt)
            return d if d.tzinfo else d.replace(tzinfo=IST)   # no zone given: NSE times are IST
        except ValueError:
            continue
    return None


def _normalise_quote(symbol: str, c: Any) -> dict[str, Any]:
    rec = _first_record(c)
    if not isinstance(rec, dict):
        raise RuntimeError("The quote came back in a shape I couldn't read.")
    ltp = _num(_pick(rec, ["ltp", "lastprice", "lasttradedprice", "last"]))   # never fall back to a close: that would pass a stale price off as live
    prev = _num(_pick(rec, ["prevprice", "previousprice", "preclose", "previousclose", "prevclose", "precloseprice", "previousclosingprice"]))
    # NOTE: the CM server's "net_price" field holds the PERCENT change (e.g. 14.36), not the rupee change,
    # so it is deliberately not read here; the rupee change is computed from ltp and the previous price.
    chg = _num(_pick(rec, ["change", "netchange", "chg", "pricechange"]))
    if ltp is not None and prev:
        chg = ltp - prev
    pct = _num(_pick(rec, ["pchange", "percentchange", "pctchange", "changepercent", "perchange", "changepct", "percentagechange"]))
    if chg is None and ltp is not None and prev:
        chg = ltp - prev
    if pct is None and chg is not None and prev:
        pct = chg / prev * 100
    hi52 = _num(_pick(rec, ["52whigh", "52weekhigh", "yearhigh", "high52", "high52w", "52wkhigh", "52wh", "wkhigh52", "fiftytwoweekhigh"]))
    lo52 = _num(_pick(rec, ["52wlow", "52weeklow", "yearlow", "low52", "low52w", "52wklow", "52wl", "wklow52", "fiftytwoweeklow"]))
    if (hi52 is None or lo52 is None) and isinstance(_pick(rec, ["52wrange", "52weekrange", "wkrange52"]), str):
        nums = re.findall(r"\d[\d,]*\.?\d*", _pick(rec, ["52wrange", "52weekrange", "wkrange52"]))
        if len(nums) >= 2:
            lo52, hi52 = sorted(float(x.replace(",", "")) for x in nums[:2])
    ts = _parse_ts(_pick(rec, ["latesttimestamp", "timestamp", "lastupdatetime", "lasttradetime", "time", "updatedat"]))
    now = dt.datetime.now(IST)
    return {
        "symbol": symbol, "ltp": ltp, "open": _num(_pick(rec, ["open", "openprice"])), "high": _num(_pick(rec, ["high", "dayhigh", "highprice"])),
        "low": _num(_pick(rec, ["low", "daylow", "lowprice"])), "prev_close": prev,
        "change": None if chg is None else round(chg, 2), "pct_change": None if pct is None else round(pct, 2),
        "volume": _num(_pick(rec, ["volume", "totaltradedvolume", "tradedvolume", "totaltradedquantity", "tradequantity", "tradedquantity", "tradeqty", "qty", "quantity", "totalqty", "totalvolume", "tradedqty"])),
        "high_52w": hi52, "low_52w": lo52, "series": _pick(rec, ["series"]),
        "time_iso": ts.isoformat() if ts else None,
        "updated": ts.strftime("%d %b %Y %H:%M:%S") if ts else (str(_pick(rec, ["latesttimestamp", "timestamp"])) if _pick(rec, ["latesttimestamp", "timestamp"]) else None),
        "age_seconds": max(0, int((now - ts).total_seconds())) if ts else None,
        "raw": rec,
    }


CMP_FRESH_SECONDS = 600       # a quote older than this during market hours is shown as "last trade", not "live"
MAX_LIVE_PRICES = 40          # most distinct stocks priced live in one evaluation


async def get_cmp(symbol: str, eod_close: Optional[float] = None, eod_date: Optional[str] = None) -> dict[str, Any]:
    """Current market price (CMP) for one stock, from the CM live feed (shared 30-second cache).

    source: "live"        market open and the quote is under 10 minutes old
            "last_trade"  market closed (or the quote is older): the last traded price and its time
            "eod"         the live feed failed or its quote is older than the end-of-day data: the last close is used
            "none"        no price at all
    Never raises: a live-feed problem must not break an analysis, so it falls back to the end-of-day close."""
    out: dict[str, Any] = {"symbol": symbol, "cmp": eod_close, "source": "eod" if eod_close is not None else "none",
                           "as_of": eod_date, "as_of_iso": None, "age_seconds": None, "change_pct": None, "prev_close": None, "note": None}
    try:
        raw = await _call_tool(SERVERS["cm"], "cm_get_stock_quote", {"symbol": symbol})
        if raw.get("is_error"):
            raise RuntimeError(str(raw.get("note") or raw.get("content"))[:150])
        q = _normalise_quote(symbol, raw.get("content"))
    except Exception as exc:  # noqa: BLE001
        out["note"] = f"Live price unavailable ({_describe_error(exc)[:120]}); using the last end-of-day close."
        return out
    if q["ltp"] is None:
        out["note"] = "The live feed returned no price; using the last end-of-day close."
        return out
    quote_day = None
    try:
        quote_day = dt.datetime.fromisoformat(q["time_iso"]).date() if q.get("time_iso") else None
        if eod_date and quote_day and quote_day < dt.date.fromisoformat(str(eod_date)[:10]):
            out["note"] = "The live feed's last quote is older than the end-of-day data; using the end-of-day close."
            return out
    except ValueError:
        pass
    live = _market_status()["state"] == "open" and q["age_seconds"] is not None and q["age_seconds"] <= CMP_FRESH_SECONDS
    out.update(cmp=q["ltp"], source="live" if live else "last_trade", as_of=q["updated"], as_of_iso=q.get("time_iso"),
               age_seconds=q["age_seconds"], change_pct=q["pct_change"], prev_close=q["prev_close"])
    return out


def _apply_cmp_to_week52(w52: dict[str, Any], cmp_price: Optional[float], info: dict[str, Any]) -> None:
    """Re-express the 52-week position at the current price (a new high or low today moves the range)."""
    if w52.get("status") != "ok" or cmp_price is None or info.get("source") not in ("live", "last_trade"):
        return
    if cmp_price > w52["high"]:
        w52["high"], w52["high_date"] = round(cmp_price, 2), (info.get("as_of_iso") or "")[:10] or w52["high_date"]
    if cmp_price < w52["low"]:
        w52["low"], w52["low_date"] = round(cmp_price, 2), (info.get("as_of_iso") or "")[:10] or w52["low_date"]
    hi, lo = w52["high"], w52["low"]
    w52.update(price=round(cmp_price, 2), price_date=(info.get("as_of_iso") or "")[:10] or w52["price_date"], price_source=info["source"],
               pct_below_high=round((hi - cmp_price) / hi * 100, 1) if hi else None,
               pct_above_low=round((cmp_price - lo) / lo * 100, 1) if lo else None,
               position_pct=round((cmp_price - lo) / (hi - lo) * 100) if hi > lo else None)


@app.get("/api/cmp")
async def cmp_prices(symbols: str):
    """Current market price for up to 20 stocks: live when the market is open, otherwise the last traded price."""
    syms = list(dict.fromkeys(x.strip().upper() for x in re.split(r"[,\s]+", symbols) if x.strip()))[:20]
    if not syms:
        raise HTTPException(400, "Give at least one symbol.")
    for x in syms:
        if not (1 <= len(x) <= 20 and all(ch.isalnum() or ch in "-&_" for ch in x)):
            raise HTTPException(400, f"'{x}' doesn't look like an NSE symbol.")
    infos = await asyncio.gather(*(get_cmp(s) for s in syms))
    return {"market": _market_status(), "prices": {i["symbol"]: i for i in infos}}


def _mover_row(r: dict[str, Any]) -> Optional[dict[str, Any]]:
    """One row of nse_get_market_movers: symbol, series, ltp, prev_price, net_price (= % change), perChange,
    trade_quantity, turnover."""
    if not isinstance(r, dict):
        return None
    sym = _pick(r, ["symbol", "scrip", "ticker"])
    ltp = _num(_pick(r, ["ltp", "lastprice", "last"]))
    prev = _num(_pick(r, ["prevprice", "previousprice", "preclose", "previousclose"]))
    pct = _num(_pick(r, ["perchange", "pchange", "percentchange", "changepercent"]))
    if sym is None or ltp is None:
        return None
    chg = ltp - prev if prev else None
    if pct is None and chg is not None and prev:
        pct = chg / prev * 100
    return {"symbol": str(sym).upper(), "series": _pick(r, ["series"]), "ltp": ltp, "prev_price": prev,
            "change": None if chg is None else round(chg, 2), "pct_change": None if pct is None else round(pct, 2),
            "volume": _num(_pick(r, ["tradequantity", "tradedquantity", "volume", "qty"])),
            "turnover": _num(_pick(r, ["turnover", "totalturnover", "tradedvalue", "value"]))}


def _movers_lists(c: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if isinstance(c, dict):
        g = next((v for k, v in c.items() if "gain" in _nk(k) and isinstance(v, list)), None)
        l = next((v for k, v in c.items() if ("loss" in _nk(k) or "lose" in _nk(k)) and isinstance(v, list)), None)
        if g is not None or l is not None:
            return [x for x in map(_mover_row, g or []) if x], [x for x in map(_mover_row, l or []) if x]
        for k in ("data", "result"):
            if isinstance(c.get(k), (dict, list)):
                return _movers_lists(c[k])
    if isinstance(c, list):                       # one mixed list: split by the sign of the change
        rows = [x for x in map(_mover_row, c) if x and x["pct_change"] is not None]
        return (sorted((x for x in rows if x["pct_change"] > 0), key=lambda x: -x["pct_change"]),
                sorted((x for x in rows if x["pct_change"] < 0), key=lambda x: x["pct_change"]))
    return [], []


@app.get("/api/movers")
async def movers(limit: int = 10, index: str = ""):
    """Today's top gainers and losers from the CM market data server."""
    idx = index if index in ("NIFTY", "BANKNIFTY", "NIFTYNEXT50") else ""
    try:
        raw = await _call_tool(SERVERS["cm"], "nse_get_market_movers", {"indexName": idx, "limit": max(1, min(limit, 50))})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, _describe_error(exc))
    if raw.get("is_error"):
        raise HTTPException(502, str(raw.get("note") or raw.get("content"))[:300])
    gainers, losers = _movers_lists(raw.get("content"))
    return {"market": _market_status(), "index": idx or "All securities", "gainers": gainers, "losers": losers,
            "fetched_at": dt.datetime.now(IST).strftime("%H:%M:%S"), "raw": raw.get("content")}


_TIME_HINTS = ("crawl", "updat", "fetch", "refresh", "timestamp", "asof", "lasttime", "datetime")


def _status_entries(content: Any, path: str = "", depth: int = 0) -> list[dict[str, Any]]:
    """Find 'last crawled / updated' style timestamps anywhere in a status response."""
    found: list[dict[str, Any]] = []
    if depth > 3:
        return found
    items = content.items() if isinstance(content, dict) else enumerate(content[:5]) if isinstance(content, list) else []
    for k, v in items:
        label = f"{path}.{k}" if path else str(k)
        if isinstance(v, (dict, list)):
            found += _status_entries(v, label, depth + 1)
        elif any(h in _nk(k) for h in _TIME_HINTS) and not isinstance(v, bool):
            if isinstance(v, (int, float)) and v < 1e9:
                continue                       # a count, not an epoch time
            ts = _parse_ts(v)
            if ts and ts.year >= 2000:
                found.append({"label": label, "when": ts.strftime("%d %b %Y %H:%M:%S"),
                              "age_seconds": max(0, int((dt.datetime.now(IST) - ts).total_seconds()))})
    return found


async def _source_status() -> dict[str, Any]:
    """How fresh is the CM server's own data? (cm_get_data_status covers gainers/losers,
    cm_get_allstocks_status covers the stock quotes.)"""
    names = {"gainers_losers": "cm_get_data_status", "all_stocks": "cm_get_allstocks_status"}
    res = await asyncio.gather(*(_call_tool(SERVERS["cm"], t, {}) for t in names.values()), return_exceptions=True)
    out: dict[str, Any] = {}
    for key, r in zip(names, res):
        if isinstance(r, BaseException):
            out[key] = {"error": _describe_error(r)}
        elif r.get("is_error"):
            out[key] = {"error": str(r.get("content"))[:200]}
        else:
            out[key] = {"entries": _status_entries(r.get("content")), "raw": r.get("content")}
    return out


@app.get("/api/live")
async def live(symbols: str):
    """Live quotes for up to 20 symbols, each next to the last end-of-day close."""
    syms = list(dict.fromkeys(x.strip().upper() for x in re.split(r"[,\s]+", symbols) if x.strip()))[:20]
    if not syms:
        raise HTTPException(400, "Give at least one symbol.")
    for x in syms:
        if not (1 <= len(x) <= 20 and all(ch.isalnum() or ch in "-&_" for ch in x)):
            raise HTTPException(400, f"'{x}' doesn't look like an NSE symbol.")

    async def one(sym: str) -> dict[str, Any]:
        try:
            raw = await _call_tool(SERVERS["cm"], "cm_get_stock_quote", {"symbol": sym})
            if raw.get("is_error"):
                raise RuntimeError(str(raw.get("note") or raw.get("content"))[:200])
            return _normalise_quote(sym, raw.get("content"))
        except Exception as exc:  # noqa: BLE001
            return {"symbol": sym, "error": _describe_error(exc)}

    async def eod() -> dict[str, dict[str, Any]]:
        try:
            raw = await _call_tool(SERVERS["bhavcopy"], "get_bulk_quote", {"symbols": syms})
            quotes = (raw.get("content") or {}).get("quotes") if isinstance(raw.get("content"), dict) else None
            return {str(q.get("symbol", "")).upper(): q for q in (quotes or [])}
        except Exception:  # noqa: BLE001 - the last close is only context
            return {}

    quotes, closes, source = await asyncio.gather(asyncio.gather(*(one(x) for x in syms)), eod(), _source_status())
    out = []
    for q in quotes:
        e = closes.get(q["symbol"])
        q["eod_close"] = _to_float(e.get("close")) if e else None
        q["eod_date"] = e.get("date") if e else None
        out.append(q)
    return {"market": _market_status(), "quotes": out, "source": source, "fetched_at": dt.datetime.now(IST).strftime("%H:%M:%S")}


@app.get("/api/nse/status")
async def nse_status():
    """Quick check of both NSE MCP servers: does a tools/list answer, and how fast."""
    out = {}
    for key, url in SERVERS.items():
        t0 = time.monotonic()
        try:
            tools = await _list_tools(url)
            out[key] = {"ok": True, "ms": round((time.monotonic() - t0) * 1000), "tools": len(tools), "url": url}
        except Exception as exc:  # noqa: BLE001
            out[key] = {"ok": False, "ms": round((time.monotonic() - t0) * 1000), "error": str(exc), "url": url}
    return out


@app.get("/api/tools", dependencies=[Depends(auth.require_admin)])
async def get_tools(server: str):
    url = _server_url(server)
    try:
        tools = await _list_tools(url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Could not reach {url}: {exc}")
    return {"server": server, "url": url, "tools": tools}


@app.post("/api/call", dependencies=[Depends(auth.require_admin)])
async def call_tool(req: CallToolRequest):
    url = _server_url(req.server)
    try:
        return await _call_tool(url, req.tool, req.arguments, use_cache=False)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Call failed: {exc}")


# ---------------------------------------------------------------------------
# History + corporate actions
# ---------------------------------------------------------------------------
async def _fetch_history(url: str, symbol: str, total_months: int, partial_ok: bool = False) -> list[dict[str, Any]]:
    """Chained 3-month chunks. With partial_ok, a failure after the first chunk returns what was
    loaded so far (the 52-week range warns when history is short) instead of failing everything."""
    records: list[dict[str, Any]] = []
    end_date = "today"
    months_left = total_months
    while months_left > 0:
        chunk = min(3, months_left)
        try:
            raw = await _call_tool(url, "get_stock_history", {"symbol": symbol, "months": chunk, "endDate": end_date})
        except Exception:
            if partial_ok and records:
                break
            raise
        if raw.get("is_error"):
            raise RuntimeError(f"get_stock_history failed: {raw.get('note') or raw.get('content')}")
        content = raw.get("content")
        data = content.get("data") if isinstance(content, dict) else None
        if not data:
            break
        records = data + records
        months_left -= chunk
        next_end = content.get("next_end_date")
        if not next_end:
            break
        end_date = next_end
    return records


async def _corporate_actions(url: str, symbol: str, first_date: str, last_date: str) -> dict[str, Any]:
    """All corporate actions with an ex-date inside the history window.

    Returns {"status": "ok"|"unavailable", "notes": [...], "adjustments": [...]}.
    Splits/bonuses with a usable factor go into `adjustments` (applied to prices);
    everything else (dividends, other events) is note-only. A failed lookup is
    reported as "unavailable" so the UI can say prices are unchecked."""
    try:
        raw = await _call_tool(url, "get_corporate_actions",
                               {"symbol": symbol, "fromDate": first_date[:10], "toDate": last_date[:10]})
        if raw.get("is_error"):
            return {"status": "unavailable", "notes": [], "adjustments": []}
        events = recommend.find_records(raw.get("content"))
    except Exception:  # noqa: BLE001
        return {"status": "unavailable", "notes": [], "adjustments": []}
    notes, adjustments = [], []
    for ev in events:
        kind = str(ev.get("actionType", "OTHER")).upper()
        factor = _to_float(ev.get("adjustmentFactor"))
        ex = str(ev.get("exDate", ""))[:10]
        if not (first_date[:10] <= ex <= last_date[:10]):
            continue
        adjusted = kind in ("SPLIT", "BONUS") and bool(factor) and 0 < factor < 1
        notes.append({"exDate": ex, "type": kind, "purpose": ev.get("purpose"),
                      "factor": factor, "adjusted": adjusted})
        if adjusted:
            adjustments.append({"exDate": ex, "type": kind, "factor": factor, "purpose": ev.get("purpose")})
    notes.sort(key=lambda n: n["exDate"])
    return {"status": "ok", "notes": notes, "adjustments": adjustments}


def _apply_adjustments(dates: list[str], closes: list[Optional[float]],
                       adjustments: list[dict[str, Any]]) -> list[Optional[float]]:
    """Scale closes before each ex-date by that event's factor (factors compound)."""
    out = list(closes)
    for i, d in enumerate(dates):
        if out[i] is None:
            continue
        for adj in adjustments:
            if d[:10] < adj["exDate"]:
                out[i] *= adj["factor"]
    return out


async def _search_symbols(url: str, query: str) -> list[dict[str, Any]]:
    raw = await _call_tool(url, "search_symbols", {"query": query})
    if raw.get("is_error"):
        raise RuntimeError(f"search_symbols failed: {raw.get('note') or raw.get('content')}")
    content = raw.get("content")
    return (content.get("results") if isinstance(content, dict) else None) or []


@app.get("/api/search")
async def search_symbol(query: str):
    url = _server_url(ANALYSIS_SERVER)
    q = query.strip()
    if not q:
        return {"query": q, "tried": [], "results": []}

    words: list[str] = []
    for w in q.replace("-", " ").split():
        if len(w) >= 3 and w.lower() not in (x.lower() for x in words):
            words.append(w)
    attempts = [q] + [w for w in words if w.lower() != q.lower()]

    tried: list[str] = []
    found: dict[str, dict[str, Any]] = {}
    try:
        for attempt in attempts:
            tried.append(attempt)
            for r in await _search_symbols(url, attempt):
                found.setdefault(r["symbol"], r)
            if found:
                break
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Symbol search failed: {exc}")

    cutoff = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    active = [r for r in found.values() if str(r.get("last_date", "")) >= cutoff]
    q_upper = q.upper().replace(" ", "")
    active.sort(key=lambda r: (r["symbol"] != q_upper, len(r["symbol"]), r["symbol"]))
    return {"query": q, "tried": tried, "hidden_inactive": len(found) - len(active), "results": active[:10]}


def _fill_position(entry: dict[str, Any]) -> None:
    """Whole-share quantity, amount invested and leftover for entry['allocation']."""
    price = entry.get("latest_price")
    alloc = entry.get("allocation") or 0.0
    qty = int(alloc // price) if price else None
    invested = round(qty * price, 2) if qty else 0.0
    entry.update({"quantity": qty, "invested": invested, "leftover": round(alloc - invested, 2)})


async def _analyze_one(url: str, stock: StockAllocation, today: str) -> dict[str, Any]:
    symbol = stock.symbol.strip().upper()
    entry: dict[str, Any] = {"symbol": symbol, "allocation": stock.allocation}
    try:
        records = await _fetch_history(url, symbol, ANALYSIS_HISTORY_MONTHS, partial_ok=True)
        if not records:
            raise ValueError("get_stock_history returned no data for this symbol.")

        dates = [str(r.get("date", "")) for r in records]
        raw_closes: list[Optional[float]] = [_to_float(r.get("close")) for r in records]
        if all(c is None for c in raw_closes):
            raise ValueError("get_stock_history returned records with no usable 'close' values.")

        ca = await _corporate_actions(url, symbol, dates[0], dates[-1])
        adjustments = ca["adjustments"]
        adj_records = _adjust_records(records, adjustments)
        closes = [_to_float(r.get("close")) for r in adj_records]

        ltp_date = next((d for d in reversed(dates) if d), today)
        ltp_raw = await _call_tool(url, "get_ltp_by_date", {"symbol": symbol, "date": ltp_date})
        if ltp_raw.get("is_error"):
            raise RuntimeError(f"get_ltp_by_date failed: {ltp_raw.get('note') or ltp_raw.get('content')}")
        latest_price = _to_float(ltp_raw.get("content")) or technical.latest_value(raw_closes)

        ma_raw = await asyncio.gather(
            _call_tool(url, "moving_average", {"symbol": symbol, "days": 20}),
            _call_tool(url, "moving_average", {"symbol": symbol, "days": 50}),
            return_exceptions=True,   # only a cross-check: a timeout here shouldn't fail the whole stock
        )
        ma20_tool, ma50_tool = [None if isinstance(x, BaseException) or x.get("is_error") else _to_float(x.get("content")) for x in ma_raw]

        macd_line, macd_signal = technical.macd(closes)
        sma20_series = technical.sma(closes, 20)
        sma50_series = technical.sma(closes, 50)
        rsi_series = technical.rsi(closes, 14)
        trade_signal = technical.generate_trade_signal(
            closes, sma20_series, sma50_series, rsi_series, macd_line, macd_signal)
        w52 = holding.week52(adj_records)
        if w52.get("status") == "ok" and ca["status"] != "ok":
            w52["unchecked"] = True  # splits/bonuses couldn't be looked up, so the range may be distorted
        hold = holding.propose(trade_signal, adj_records, w52)
        # Second rule set: support and resistance. It is shown next to the trend signal; funding and allocation
        # keep using the trend signal, so nothing there changes.
        sr_p = backtest.prepare(adj_records)
        sr_signal = sr_model.generate_sr_signal(sr_p["o"], sr_p["h"], sr_p["l"], sr_p["c"])
        sr_hold = holding.propose(sr_signal, adj_records, w52, model="sr")

        # The signal's entry/stop/target come from adjusted closes; the latest
        # close is after any ex-date, so they're on the same scale as the LTP.
        # The signal and indicators stay on end-of-day closes (that is how the rules are defined). The current
        # market price is used for sizing, cost, the 52-week position, and a check that the setup still holds.
        eod_close = latest_price
        cmp_info = await get_cmp(symbol, eod_close, ltp_date[:10])
        cmp_price = cmp_info["cmp"] if cmp_info["cmp"] is not None else eod_close
        long_setup = trade_signal.get("signal") == "LONG" and cmp_price is not None
        cmp_check = technical.check_price(trade_signal["entry"], trade_signal["stop_loss"], trade_signal["target"], cmp_price) if long_setup else None
        buy_price = cmp_price if cmp_check and cmp_check["status"] == "ok" else None
        sr_check = (technical.check_price(sr_signal["entry"], sr_signal["stop_loss"], sr_signal["target"], cmp_price)
                    if sr_signal.get("signal") == "LONG" and cmp_price is not None else None)
        _apply_cmp_to_week52(w52, cmp_price, cmp_info)
        try:
            ltp_age_days = (dt.date.fromisoformat(today) - dt.date.fromisoformat(ltp_date[:10])).days
        except ValueError:
            ltp_age_days = None

        entry.update({
            "dates": dates, "close": closes, "sma20": sma20_series, "sma50": sma50_series,
            "rsi": rsi_series, "macd_line": macd_line, "macd_signal": macd_signal,
            "moving_average_20": ma20_tool, "moving_average_50": ma50_tool,
            "latest_price": cmp_price, "eod_close": eod_close, "ltp_date": ltp_date, "ltp_age_days": ltp_age_days,
            "cmp": cmp_price, "cmp_source": cmp_info["source"], "cmp_as_of": cmp_info["as_of"], "cmp_age_seconds": cmp_info["age_seconds"],
            "cmp_change_pct": cmp_info["change_pct"], "cmp_note": cmp_info["note"], "cmp_check": cmp_check, "buy_price": buy_price,
            "annual_vol_pct": None if (vol := allocation.annual_vol(closes)) is None else round(vol * 100, 1),
            "trade_signal": trade_signal, "week52": w52, "hold": hold, "corporate_adjustments": adjustments,
            "sr_signal": sr_signal, "sr_cmp_check": sr_check, "sr_hold": sr_hold,
            "model_agreement": technical.compare_signals(trade_signal, sr_signal),
            "ca_status": ca["status"], "ca_notes": ca["notes"],
        })
        _fill_position(entry)
    except Exception as exc:  # noqa: BLE001
        entry["error"] = _describe_error(exc)
    return entry


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest, user: dict = Depends(auth.current_user)):
    if req.signal_model not in ("trend", "sr"):
        raise HTTPException(400, "signal_model must be 'trend' or 'sr'.")
    url = _server_url(ANALYSIS_SERVER)
    today = dt.date.today().isoformat()
    sem = asyncio.Semaphore(ANALYSIS_CONCURRENCY)

    async def run(stock: StockAllocation) -> dict[str, Any]:
        async with sem:
            return await _analyze_one(url, stock, today)

    results = list(await asyncio.gather(*(run(s) for s in req.stocks)))
    ok = [r for r in results if "error" not in r]

    unallocated, cash, weights = None, 0.0, {}
    if req.mode == "suggested" and ok and req.total_capital > 0:
        sugg, cash = allocation.suggest(
            [{"symbol": r["symbol"], "vol": (r["annual_vol_pct"] / 100) if r.get("annual_vol_pct") else None,
              "signal": (r["sr_signal"] if req.signal_model == "sr" else r["trade_signal"]).get("signal")} for r in ok],
            max(5.0, min(req.max_weight_pct, 100.0)))
        for r in ok:
            w = sugg[r["symbol"]]
            weights[r["symbol"]] = w["weight"]
            r["allocation"] = round(req.total_capital * w["weight"], 2)
            r["weight_pct"] = round(w["weight"] * 100, 1)
            r["alloc_basis"] = {"vol_pct": None if not w["vol"] else round(w["vol"] * 100, 1),
                                "vol_estimated": w["vol_estimated"], "basis": w["basis"]}
            _fill_position(r)
        unallocated = {"pct": round(cash * 100, 1), "amount": round(req.total_capital * cash, 2)}
    else:
        for r in ok:
            weights[r["symbol"]] = (r["allocation"] / req.total_capital) if req.total_capital > 0 else 0.0
            r["weight_pct"] = round(100 * weights[r["symbol"]], 1) if req.total_capital > 0 else None

    # Funding: total_capital is OWN capital. With MTF the positions are bigger than the capital.
    rules = await asyncio.to_thread(funding.load_rules, user["id"])
    plan = None
    if req.use_mtf and ok and req.total_capital > 0:
        plan = funding.plan_exposure(
            [{"weight": weights[r["symbol"]], "own": funding.margin_for(r["symbol"], rules)["own"]} for r in ok],
            req.total_capital, req.mtf_limit)
        for r in ok:
            r["allocation"] = round(weights[r["symbol"]] * plan["exposure"], 2)
            _fill_position(r)
        if unallocated is not None:
            unallocated = {"pct": round(cash * 100, 1), "amount": round(cash * plan["exposure"], 2)}
    for r in ok:
        view = r
        if req.signal_model == "sr":      # size and cost the support/resistance setup instead of the trend one
            ok_now = r.get("sr_cmp_check") and r["sr_cmp_check"]["status"] == "ok"
            view = {**r, "trade_signal": r["sr_signal"], "hold": r["sr_hold"], "buy_price": r["cmp"] if ok_now else None}
        r["funding"] = funding.stock_funding(view, rules, req.use_mtf)
    fsum = funding.summarize(ok, req.total_capital, plan, req.mtf_limit, req.use_mtf, rules)
    fsum["signal_model"] = req.signal_model
    return {"mode": "suggested" if unallocated is not None else "equal", "unallocated": unallocated,
            "funding": fsum, "results": results}


def _adjust_records(records: list[dict[str, Any]], adjustments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply split/bonus factors to open/high/low/close of every bar before each ex-date."""
    if not adjustments:
        return records
    dates = [str(r.get("date", "")) for r in records]
    keys = {k for r in records for k in r if str(k).lower() in ("open", "high", "low", "close")}
    cols = {k: _apply_adjustments(dates, [_to_float(r.get(k)) for r in records], adjustments) for k in keys}
    return [{**r, **{k: cols[k][i] for k in keys if k in r}} for i, r in enumerate(records)]


async def _fetch_history_adjusted(url: str, symbol: str, total_months: int, partial_ok: bool = False) -> list[dict[str, Any]]:
    """Like _fetch_history, but split/bonus-adjusts open/high/low/close so screening
    and backtests aren't distorted by ex-date price drops."""
    records = await _fetch_history(url, symbol, total_months, partial_ok)
    if not records:
        return records
    dates = [str(r.get("date", "")) for r in records]
    ca = await _corporate_actions(url, symbol, dates[0], dates[-1])
    return _adjust_records(records, ca["adjustments"])


@app.post("/api/recommend")
async def recommend_stocks(req: RecommendRequest, user: dict = Depends(auth.current_user)):
    """One scan per user at a time, and only NSE_HEAVY_JOBS across everyone: the rest wait their turn."""
    if req.model not in recommend.MODELS:
        raise HTTPException(400, f"model must be one of {list(recommend.MODELS)}.")
    if user["id"] in _scanning:
        raise HTTPException(409, "You already have a scan running. Wait for it to finish.")
    _scanning.add(user["id"])
    try:
        async with heavy_slot():
            return await _recommend_inner(req, user)
    finally:
        _scanning.discard(user["id"])


async def _recommend_inner(req: RecommendRequest, user: dict):
    url = _server_url(ANALYSIS_SERVER)
    try:
        result = await recommend.recommend(url, req.existing, req.budget, _fetch_history_adjusted, _call_tool, price_lookup=get_cmp, model=req.model)
        sem = asyncio.Semaphore(ANALYSIS_CONCURRENCY)

        async def enrich(p: dict[str, Any]) -> None:
            async with sem:
                try:
                    recs = await _fetch_history_adjusted(url, p["symbol"], ANALYSIS_HISTORY_MONTHS, partial_ok=True)
                    sig = {"signal": "LONG", "entry": p["entry"], "stop_loss": p["stop_loss"], "target": p["target"]}
                    p["week52"] = holding.week52(recs)
                    p["hold"] = holding.propose(sig, recs, p["week52"], model=p.get("model", "trend"))
                    _apply_cmp_to_week52(p["week52"], p.get("cmp"), {"source": p.get("cmp_source"), "as_of_iso": None})
                    if p["hold"]["status"] == "proposed":
                        p["hold_days"] = p["hold"]["days"]
                except Exception as exc:  # noqa: BLE001 - suggestions still show without these extras
                    p["week52"] = {"status": "no_data"}
                    p["hold"] = {"status": "none", "days": None, "review_by": None, "confidence": None, "evidence": {},
                                 "reasoning": [f"The 12 months of history needed for the 52-week range and holding period couldn't be loaded ({_describe_error(exc)})."]}

        await asyncio.gather(*(enrich(p) for p in result.get("picks", [])))
        return result
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Recommendation failed: {_describe_error(exc)}")


# ---------------------------------------------------------------------------
# Evaluation: backtest, and the forward check of the list the user keeps on their own computer
# ---------------------------------------------------------------------------
class BacktestRequest(BaseModel):
    symbols: list[str]
    months: int = 12
    max_hold: int = 20
    cost_pct: float = 0.2
    strategy: str = "trend"       # "trend" (SMA/MACD/RSI) or "sr" (support and resistance)


@app.post("/api/backtest")
async def run_backtest(req: BacktestRequest):
    url = _server_url(ANALYSIS_SERVER)
    symbols = list(dict.fromkeys(s.strip().upper() for s in req.symbols if s.strip()))[:10]
    if not symbols:
        raise HTTPException(400, "Give at least one symbol.")
    months = max(6, min(req.months, 24))
    max_hold = max(2, min(req.max_hold, 120))
    cost = max(0.0, min(req.cost_pct, 5.0))
    if req.strategy not in ("trend", "sr"):
        raise HTTPException(400, "strategy must be 'trend' or 'sr'.")
    signal_fn = sr_model.signal_from_prepared if req.strategy == "sr" else None
    sem = asyncio.Semaphore(ANALYSIS_CONCURRENCY)

    async def one(sym: str) -> dict[str, Any]:
        async with sem:
            try:
                records = await _fetch_history_adjusted(url, sym, months)
                if not records:
                    raise ValueError("No price history returned.")
                return {"symbol": sym, **backtest.run(records, max_hold, cost, signal_fn=signal_fn)}
            except Exception as exc:  # noqa: BLE001
                return {"symbol": sym, "error": _describe_error(exc)}

    results = await asyncio.gather(*(one(s) for s in symbols))
    ok = [r for r in results if "error" not in r]
    return {"params": {"months": months, "max_hold": max_hold, "cost_pct": cost, "strategy": req.strategy},
            "results": results, "pooled": backtest.pool(ok) if ok else None}


# ---------------------------------------------------------------------------
# Sector momentum (see sectors.py): equal-weight sector indices from each sector's large stocks
# ---------------------------------------------------------------------------
SECTOR_TTL = float(os.getenv("SECTOR_TTL", "900"))      # seconds a built sector is reused
SECTOR_LIVE_TTL = float(os.getenv("SECTOR_LIVE_TTL", "30"))   # seconds the live-price overlay is reused
_sector_cache: dict[str, tuple[float, dict[str, Any]]] = {}      # finished result with live prices
_sector_base: dict[str, tuple[float, dict[str, list[dict[str, Any]]], dict[str, str]]] = {}   # end-of-day history
_sector_locks: dict[str, asyncio.Lock] = {}


async def _sector_data(name: str) -> dict[str, Any]:
    """History (end of day) is reused for SECTOR_TTL; the live-price overlay is refreshed whenever the result is older than SECTOR_LIVE_TTL."""
    catalogue = sectors.load_sectors()
    if name not in catalogue:
        raise HTTPException(404, "Unknown sector.")
    async with _sector_locks.setdefault(name, asyncio.Lock()):        # one build per sector at a time
        hit = _sector_cache.get(name)
        if hit and time.time() - hit[0] < SECTOR_LIVE_TTL:
            return hit[1]
        base = _sector_base.get(name)
        if base is None or time.time() - base[0] >= SECTOR_TTL:
            url = _server_url(ANALYSIS_SERVER)
            sem = asyncio.Semaphore(ANALYSIS_CONCURRENCY)
            records: dict[str, list[dict[str, Any]]] = {}
            errors: dict[str, str] = {}

            async def one(sym: str) -> None:
                async with sem:
                    try:
                        recs = await _fetch_history_adjusted(url, sym, 12, partial_ok=True)
                        if recs:
                            records[sym] = recs
                        else:
                            errors[sym] = "no price history"
                    except Exception as exc:  # noqa: BLE001
                        errors[sym] = _describe_error(exc)[:120]

            async with heavy_slot():
                await asyncio.gather(*(one(s) for s in sectors.all_symbols(catalogue[name])))
            base = _sector_base[name] = (time.time(), records, errors)
        _, records, errors = base
        live: dict[str, dict[str, Any]] = {}
        psem = asyncio.Semaphore(6)

        async def price(sym: str) -> None:
            last = records[sym][-1]
            async with psem:
                live[sym] = await get_cmp(sym, _to_float(last.get("close")), str(last.get("date", ""))[:10] or None)

        await asyncio.gather(*(price(s) for s in records))             # get_cmp never raises; shared 30-second cache
        data = sectors.assemble(name, catalogue[name], records, dict(errors), live)
        data["market"] = _market_status()["state"]
        data["fetched_at"] = dt.datetime.now(IST).strftime("%d %b %H:%M:%S")
        _sector_cache[name] = (time.time(), data)
        return data


@app.get("/api/sectors/one")
async def sectors_one(sector: str):
    """One sector: its index (daily, weekly, monthly), momentum numbers and constituent stocks."""
    try:
        return await _sector_data(sector.strip())
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Sector data failed: {_describe_error(exc)}")


@app.get("/api/sectors/summary")
async def sectors_summary(cap: str = "all"):
    """Ranking of the sectors loaded so far (no NSE calls). cap: all, large or midsmall. The page loads sectors one by one, then asks for this."""
    if cap not in sectors.CAPS:
        raise HTTPException(400, f"cap must be one of {list(sectors.CAPS)}.")
    catalogue = sectors.load_sectors()
    loaded = {n: v[1] for n, v in _sector_cache.items() if n in catalogue}
    return {"names": list(catalogue), "sectors": sectors.rank(loaded, cap), "industries": sectors.rank_industries(loaded, cap),
            "map_file": sectors.map_status(),
            "meta": {n: {"fetched_at": v[1]["fetched_at"], "age_s": int(time.time() - v[0]), "market": v[1]["market"]}
                     for n, v in _sector_cache.items() if n in catalogue}}


@app.get("/api/sectors/rotation")
async def sectors_rotation(tf: str = "weekly", cap: str = "all", level: str = "sectors", sector: str = ""):
    """Sector rotation points from the sectors already loaded (no NSE calls). level: sectors, or industries (of `sector`)."""
    if tf not in sectors.TFS or cap not in sectors.CAPS or level not in ("sectors", "industries"):
        raise HTTPException(400, "Bad tf, cap or level.")
    catalogue = sectors.load_sectors()
    return sectors.rotation({n: v[1] for n, v in _sector_cache.items() if n in catalogue}, tf, cap, level, sector.strip())


@app.get("/api/sectors/picks")
async def sectors_picks(cap: str = "all", tf: str = "weekly", limit: int = 15):
    """A mechanical shortlist of stocks to review, from the sectors already loaded (no NSE calls)."""
    if tf not in sectors.TFS or cap not in sectors.CAPS:
        raise HTTPException(400, "Bad tf or cap.")
    catalogue = sectors.load_sectors()
    return sectors.picks({n: v[1] for n, v in _sector_cache.items() if n in catalogue}, cap, tf, max(1, min(limit, 40)))


MAX_EVAL_ENTRIES = 300


class EvalEntry(BaseModel):
    """One entry of the user's evaluation list, sent from their browser. Nothing here is stored."""
    id: str
    symbol: str
    as_of: str
    entry: float
    stop_loss: float
    target: float
    source: str = "user"
    hold_days: Optional[int] = None
    taken: bool = False
    score: Optional[str] = None
    logged_at: Optional[str] = None
    model: str = "trend"            # rule set that produced the setup: "trend" or "sr"
    setup: Optional[str] = None


class EvaluateRequest(BaseModel):
    entries: list[EvalEntry]
    max_hold: int = 20
    cost_pct: float = 0.2


@app.post("/api/track/evaluate")
async def track_evaluate(req: EvaluateRequest, user: dict = Depends(auth.current_user)):
    """How each entry on the user's evaluation list has played out since it was added.

    The list lives in the user's own browser (and, if they choose, a JSON file on their computer). The
    browser sends it here, prices are checked against the shared NSE data, and the results go straight
    back: the server stores nothing. Only entries with entry/stop/target levels are sent."""
    if len(req.entries) > MAX_EVAL_ENTRIES:
        raise HTTPException(400, f"Send at most {MAX_EVAL_ENTRIES} stocks at a time.")
    entries: list[dict[str, Any]] = []
    for e in req.entries:
        sym = e.symbol.strip().upper()
        if not (1 <= len(sym) <= 20 and all(ch.isalnum() or ch in "-&_" for ch in sym)):
            raise HTTPException(400, f"'{e.symbol}' doesn't look like an NSE symbol.")
        try:
            dt.date.fromisoformat(e.as_of[:10])
        except ValueError:
            raise HTTPException(400, f"Bad date for {sym}.")
        if not (0 < e.stop_loss < e.entry < e.target):
            raise HTTPException(400, f"{sym}: need 0 < stop < entry < target.")
        if len(e.id) > 40:
            raise HTTPException(400, "An entry id is too long.")
        entries.append({**e.model_dump(), "symbol": sym, "as_of": e.as_of[:10], "model": "sr" if e.model == "sr" else "trend"})
    if not entries:
        return {"entries": []}

    url = _server_url(ANALYSIS_SERVER)
    today = dt.date.today()
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        by_symbol.setdefault(e["symbol"], []).append(e)
    sem = asyncio.Semaphore(ANALYSIS_CONCURRENCY)

    async def history(sym: str, group: list[dict[str, Any]]):
        oldest = min(dt.date.fromisoformat(e["as_of"]) for e in group)
        months = max(1, min(6, (today - oldest).days // 30 + 2))
        async with sem:
            try:
                return sym, await _fetch_history_adjusted(url, sym, months), None
            except Exception as exc:  # noqa: BLE001
                return sym, None, _describe_error(exc)

    max_hold = max(2, min(req.max_hold, 120))
    cost = max(0.0, min(req.cost_pct, 5.0))
    async with heavy_slot():
        hist = {sym: (recs, err) for sym, recs, err in await asyncio.gather(*(history(s, g) for s, g in by_symbol.items()))}

    rows = []
    for e in entries:
        recs, err = hist[e["symbol"]]
        if not recs:
            ev = {"status": "no_data", "error": err}
        else:
            ev = backtest.evaluate_forward(recs, e["as_of"], e["entry"], e["stop_loss"], e["target"],
                                           max(2, min(e.get("hold_days") or max_hold, 120)), cost)
        rows.append({**e, **ev})

    # Trades still open: show them at the current market price. A stop or target touched today is flagged, but the
    # status stays as it is until the day's bars confirm it.
    open_syms = list(dict.fromkeys(r["symbol"] for r in rows if r.get("status") in ("open", "pending")))[:MAX_LIVE_PRICES]
    if open_syms:
        async def price(sym: str):
            recs = hist[sym][0] or []
            last = recs[-1] if recs else {}
            async with sem:
                return sym, await get_cmp(sym, _to_float(last.get("close")), str(last.get("date", ""))[:10] or None)
        prices = dict(await asyncio.gather(*(price(s) for s in open_syms)))
        for r in rows:
            info = prices.get(r["symbol"])
            if r.get("status") not in ("open", "pending") or not info:
                continue
            r["price_source"], r["price_as_of"] = info["source"], info["as_of"]
            if info["source"] not in ("live", "last_trade"):
                continue
            recs = hist[r["symbol"]][0]
            closes_by_date = {str(x.get("date", ""))[:10]: _to_float(x.get("close")) for x in recs}
            base = max((d for d in closes_by_date if d <= r["as_of"]), default=None)
            scale = (closes_by_date[base] / r["entry"]) if base and closes_by_date[base] and r["entry"] else 1.0
            entry, stop, target = r["entry"] * scale, r["stop_loss"] * scale, r["target"] * scale   # same basis as the adjusted prices
            r["current_price"] = round(info["cmp"], 2)
            r["return_pct"] = round((info["cmp"] / entry - 1) * 100, 2)
            r["live_alert"] = "below_stop" if info["cmp"] <= stop else "at_target" if info["cmp"] >= target else None
    return {"entries": rows}


# Optional add-on: the chat assistant widget (needs anthropic + ANTHROPIC_API_KEY).
# Same origin as this dashboard, so no CORS setup. Local use only: do not deploy server.py publicly.
try:
    import agent_routes
    import settings_routes

    app.include_router(agent_routes.build_router(detailed=False))  # signed-in users see generic errors; details stay in the server log
    app.include_router(settings_routes.router)                     # local: API key / model settings
except ImportError as _exc:  # keep the dashboard working without the agent's packages
    print(f"[dashboard] Assistant widget disabled: {_exc}")

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

if __name__ == "__main__":
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("PORT") or os.getenv("DASHBOARD_PORT") or 8000)
    if host not in ("127.0.0.1", "localhost", "::1") and not auth.AUTH_ENABLED:
        raise SystemExit("Refusing to listen on a public address without a password. "
                         "Run `python make_password_hash.py`, add its two lines to .env, and try again.")
    uvicorn.run("server:app", host=host, port=port)
