"""Tool handlers for the agent. They reuse the dashboard's server.py logic and
return compact JSON (no full price series) so the model's context stays small."""
import asyncio
import datetime as dt
import functools
import math
from typing import Any, Optional

import backtest
import recommend
import sectors
import server as core
import sr_model
import technical


def _url() -> str:
    return core._server_url(core.ANALYSIS_SERVER)


def _sym(s: str) -> str:
    s = (s or "").strip().upper()
    if not s or len(s) > 20 or not all(ch.isalnum() or ch in "-&_" for ch in s):
        raise ValueError(f"'{s}' doesn't look like an NSE symbol.")
    return s


async def search_symbol(query: str) -> dict[str, Any]:
    r = await core.search_symbol(query[:60])
    return {"matches": [{k: m.get(k) for k in ("symbol", "last_close", "pct_change", "last_date")} for m in r["results"]],
            "note": "Search matches ticker text, not full company names. No matches: try the ticker or a shorter keyword."}


async def analyze_stock(symbol: str) -> dict[str, Any]:
    sym = _sym(symbol)
    today = dt.date.today().isoformat()
    e = await core._analyze_one(_url(), core.StockAllocation(symbol=sym, allocation=0), today)
    if e.get("error"):
        raise RuntimeError(e["error"])
    lv = technical.latest_value
    return {
        "symbol": sym, "cmp": e["cmp"], "cmp_source": e["cmp_source"], "cmp_as_of": e["cmp_as_of"], "cmp_change_pct": e["cmp_change_pct"],
        "cmp_check": e["cmp_check"], "cmp_note": e["cmp_note"],
        "last_close": e["eod_close"], "last_close_date": e["ltp_date"], "data_age_days": e["ltp_age_days"],
        "sma20": _r(lv(e["sma20"])), "sma50": _r(lv(e["sma50"])), "rsi14": _r(lv(e["rsi"])),
        "macd": _r(lv(e["macd_line"])), "macd_signal": _r(lv(e["macd_signal"])),
        "server_ma20_unadjusted": e["moving_average_20"], "server_ma50_unadjusted": e["moving_average_50"],
        "signal": e["trade_signal"],
        "sr_signal": e.get("sr_signal"), "sr_cmp_check": e.get("sr_cmp_check"), "model_agreement": e.get("model_agreement"),
        "sr_holding_period": {k: e["sr_hold"][k] for k in ("status", "days", "review_by", "confidence", "reasoning")} if e.get("sr_hold") else None,
        "week52": e["week52"],
        "holding_period": {k: e["hold"][k] for k in ("status", "days", "review_by", "confidence", "reasoning")},
        "corporate_actions_last_12m": e["ca_notes"] if e["ca_status"] == "ok" else "could not be fetched",
    }


def _r(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 2)


async def backtest_stock(symbol: str, months: int = 12, max_hold_days: int = 20, cost_pct: float = 0.2, strategy: str = "trend") -> dict[str, Any]:
    sym = _sym(symbol)
    records = await core._fetch_history_adjusted(_url(), sym, max(6, min(int(months), 24)))
    if not records:
        raise RuntimeError("No price history returned.")
    use_sr = str(strategy).lower() == "sr"
    r = backtest.run(records, max(2, min(int(max_hold_days), 120)), max(0.0, min(float(cost_pct), 5.0)),
                     signal_fn=sr_model.signal_from_prepared if use_sr else None)
    return {"symbol": sym, "strategy": "support_resistance" if use_sr else "trend_momentum", "history_from": r["dates"][0], "history_to": r["dates"][-1], "metrics": r["metrics"],
            "recent_trades": r["trades"][-8:],
            "caveat": "Backtest uses the same stock selection hindsight; few trades prove little."}


async def suggest_stocks(budget: float, holdings: Optional[list[str]] = None, model: str = "trend") -> dict[str, Any]:
    model = model if model in recommend.MODELS else "trend"
    url = _url()
    syms = list(dict.fromkeys(_sym(s) for s in (holdings or [])))[:10]

    async def hist(s: str) -> dict[str, Any]:
        recs = await core._fetch_history_adjusted(url, s, 6)
        return {"symbol": s, "dates": [str(r.get("date", "")) for r in recs],
                "close": [core._to_float(r.get("close")) for r in recs]}

    existing = list(await asyncio.gather(*(hist(s) for s in syms))) if syms else []
    # Not logged: on a public site the track-record file would mix every visitor's picks.
    async with core.heavy_slot():       # shared with the dashboard's scan: a few at a time across all users
        return await recommend.recommend(url, existing, max(0.0, float(budget)), core._fetch_history_adjusted, core._call_tool,
                                         price_lookup=core.get_cmp, model=model)


async def live_quote(symbol: str) -> Any:
    raw = await core._call_tool(core.SERVERS["cm"], "cm_get_stock_quote", {"symbol": _sym(symbol)})
    if raw.get("is_error"):
        raise RuntimeError(str(raw.get("content"))[:300])
    return raw["content"]


async def market_movers(limit: int = 10, index: str = "") -> Any:
    raw = await core._call_tool(core.SERVERS["cm"], "nse_get_market_movers",
                                {"indexName": index if index in ("NIFTY", "BANKNIFTY", "NIFTYNEXT50") else "",
                                 "limit": max(1, min(int(limit), 50))})
    if raw.get("is_error"):
        raise RuntimeError(str(raw.get("content"))[:300])
    return raw["content"]


async def sector_momentum(sector: str = "", cap: str = "all") -> dict[str, Any]:
    """One sector's momentum (fresh read, about a minute the first time) or a ranking of sectors already loaded."""
    cap = cap if cap in sectors.CAPS else "all"
    if sector.strip():
        catalogue = sectors.load_sectors()
        names = {n.lower(): n for n in catalogue}
        q = sector.strip().lower()
        name = names.get(q) or next((n for low, n in names.items() if q in low), None)
        if not name:                                  # allow an industry group name, e.g. "banks" or "auto"
            name = next((n for n, inds in catalogue.items() if any(q in i.lower() for i in inds)), None)
        if not name:
            return {"error": f"Unknown sector '{sector}'.", "sectors": list(names.values())}
        d = await core._sector_data(name)
        seg = d["segments"].get(cap)
        if seg is None:
            return {"sector": name, "note": f"No {cap} stocks in this sector."}
        keep = [s for s in d["stocks"] if cap == "all" or (cap == "large") == (s["cap"] == "large")]
        return {"sector": name, "cap_view": cap, "as_of": d["as_of"], "history_to": d["history_to"], "price_coverage": d["live"], "fetched_at": d["fetched_at"], "status": sectors.status(seg["metrics"]), "metrics": seg["metrics"],
                "stocks": [{k: s[k] for k in ("symbol", "cap", "industry", "m1", "w1", "above_sma50", "trend_long", "sr_long")} for s in keep[:10]],
                "industries": [{k: r[k] for k in ("industry", "status", "score", "w1", "m1", "m3", "breadth50")} for r in sectors.rank_industries({name: d}, cap)],
                "not_included": d["errors"],
                "note": "Equal-weight proxy index of the sector's stocks (not an official NSE index). 1D is the latest traded price against the previous close; history is end of day. Status is a mechanical rule on past prices."}
    loaded = {n: v[1] for n, v in core._sector_cache.items()}
    rows = sectors.rank(loaded, cap)
    return {"cap_view": cap, "sectors": [{k: r[k] for k in ("name", "status", "score", "w1", "m1", "m3", "breadth50")} for r in rows],
            "note": "Only sectors already loaded in this server are ranked. Ask about a sector by name for a fresh read." if rows
                    else "No sectors are loaded yet. Ask about a specific sector by name, or open the Sectors tab and click Load sectors."}


async def sector_rotation(sector: str = "", tf: str = "weekly", cap: str = "all", level: str = "sectors") -> dict[str, Any]:
    """Which sectors are Leading, Improving, Weakening or Lagging against the others, and the early movers.
    level 'sectors' uses the sectors already loaded in this server (no NSE calls); 'industries' needs a sector name
    and loads that sector if it isn't loaded yet (about a minute)."""
    tf = tf if tf in sectors.TFS else "weekly"
    cap = cap if cap in sectors.CAPS else "all"
    level = "industries" if level == "industries" else "sectors"
    catalogue = sectors.load_sectors()
    q, names = sector.strip().lower(), {n.lower(): n for n in catalogue}
    name = (names.get(q) or next((n for low, n in names.items() if q and q in low), None)
            or next((n for n, inds in catalogue.items() if q and any(q in i.lower() for i in inds)), None))
    if level == "industries":
        if not name:
            return {"error": "Give a sector name for the industry-group view.", "sectors": list(catalogue)}
        await core._sector_data(name)
    loaded = {n: v[1] for n, v in core._sector_cache.items() if n in catalogue}
    r = sectors.rotation(loaded, tf, cap, level, name or "")
    return {"tf": tf, "cap_view": cap, "level": level, "sector": name, "benchmark": r["benchmark"], "warning": r["warning"],
            "sectors_loaded": len(loaded), "counts": r.get("counts"), "early_movers": [i["name"] for i in r["items"] if i["early"]],
            "items": [{k: i[k] for k in ("name", "quadrant", "prev_quadrant", "ratio", "mom", "signal", "early", "as_of")} for i in r["items"]],
            "note": "Simplified relative-rotation view of equal-weight proxy indices against the equal-weight of the sectors loaded "
                    "(not the Nifty 50, not the licensed RRG). The newest point includes live prices and is partial until the period ends. "
                    "Rotations often fail: confirm with the stocks themselves."}


async def sector_picks(sector: str = "", cap: str = "all", tf: str = "weekly", limit: int = 10) -> dict[str, Any]:
    """The 'Stocks to review' shortlist from the sectors already loaded on the server (no NSE calls). With a sector
    name, that sector is loaded first if needed (about a minute) and only its stocks are returned."""
    tf = tf if tf in sectors.TFS else "weekly"
    cap = cap if cap in sectors.CAPS else "all"
    limit = max(1, min(int(limit), 20))
    catalogue = sectors.load_sectors()
    q, names = sector.strip().lower(), {n.lower(): n for n in catalogue}
    name = None
    if q:
        name = (names.get(q) or next((n for low, n in names.items() if q in low), None)
                or next((n for n, inds in catalogue.items() if any(q in i.lower() for i in inds)), None))
        if not name:
            return {"error": f"Unknown sector '{sector}'.", "sectors": list(catalogue)}
        await core._sector_data(name)
    loaded = {n: v[1] for n, v in core._sector_cache.items() if n in catalogue}
    r = sectors.picks(loaded, cap, tf, 40 if name else limit)
    rows = [p for p in r["picks"] if not name or p["sector"] == name][:limit]
    return {"cap_view": cap, "tf": tf, "sector": name, "sectors_loaded": len(loaded), "candidates": r["candidates"],
            "rotation_warning": r["rotation_warning"],
            "picks": [{"symbol": p["symbol"], "sector": p["sector"], "industry": p["industry"], "cap": p["cap"], "score": p["score"],
                       "price": p["close"], "d1_pct": p["d1"], "w1_pct": p["w1"], "m1_pct": p["m1"], "vs_sector_pct": p["vs_sector"],
                       "trend_long": p["trend_long"], "sr_long": p["sr_long"], "reasons": p["reasons"], "cautions": p["cautions"]} for p in rows],
            "note": ("A mechanical shortlist from sector momentum, rotation and the app's rule setups on past prices; not a recommendation. "
                     "LONG setups come from the last close, so check the current price. Only sectors already loaded are considered; "
                     "no sectors loaded means an empty list: ask the user to open the Sectors tab and click Load sectors, or name a sector.")}


async def sr_levels(symbol: str) -> dict[str, Any]:
    """Support/resistance zones and the rule-based support/resistance setup for one stock."""
    sym = _sym(symbol)
    recs = await core._fetch_history_adjusted(_url(), sym, 12)
    if not recs:
        raise RuntimeError("No price history returned.")
    p = backtest.prepare(recs)
    sig = sr_model.generate_sr_signal(p["o"], p["h"], p["l"], p["c"])
    info = await core.get_cmp(sym, p["c"][-1], p["dates"][-1])
    if sig["signal"] == "LONG" and info["cmp"] is not None:
        sig["cmp_check"] = technical.check_price(sig["entry"], sig["stop_loss"], sig["target"], info["cmp"])
    return {"symbol": sym, "last_close": round(p["c"][-1], 2), "last_close_date": p["dates"][-1],
            "cmp": info["cmp"], "cmp_source": info["source"], "cmp_as_of": info["as_of"], "sr_signal": sig}


HANDLERS = {
    "search_symbol": search_symbol, "analyze_stock": analyze_stock, "backtest_stock": backtest_stock,
    "suggest_stocks": suggest_stocks, "live_quote": live_quote, "market_movers": market_movers,
    "sr_levels": sr_levels, "sector_momentum": sector_momentum, "sector_rotation": sector_rotation, "sector_picks": sector_picks,
}


def clean_watchlist(raw: Any) -> list[dict[str, Any]]:
    """The evaluation list arrives from the user's browser, so it is treated as untrusted data: only known
    fields are kept, symbols and dates are checked, numbers must be finite, and nothing free-form gets through."""
    def number(v: Any) -> Optional[float]:
        return round(float(v), 2) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None

    out: list[dict[str, Any]] = []
    for r in (raw if isinstance(raw, list) else [])[:50]:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol", "")).strip().upper()
        if not (1 <= len(sym) <= 20 and all(ch.isalnum() or ch in "-&_" for ch in sym)):
            continue
        as_of = str(r.get("as_of", ""))[:10]
        try:
            dt.date.fromisoformat(as_of)
        except ValueError:
            continue
        hold = r.get("hold_days")
        out.append({"symbol": sym, "source": "system" if r.get("source") == "system" else "user", "as_of": as_of,
                    "entry": number(r.get("entry")), "stop_loss": number(r.get("stop_loss")), "target": number(r.get("target")),
                    "hold_days": hold if isinstance(hold, int) and not isinstance(hold, bool) and 1 <= hold <= 120 else None,
                    "taken": r.get("taken") is True, "model": "sr" if r.get("model") == "sr" else "trend"})
    return out


async def my_watchlist(watchlist: list[dict[str, Any]], source: str = "", limit: int = 30) -> dict[str, Any]:
    """The evaluation list the user's own browser shared with this chat. `watchlist` is bound by handlers_for(),
    never supplied by the model, and is already cleaned by clean_watchlist()."""
    rows = [r for r in watchlist if source not in ("user", "system") or r["source"] == source]
    rows.sort(key=lambda r: (r["as_of"], r["symbol"]), reverse=True)
    rows = rows[:max(1, min(int(limit), 50))]
    # Current market price for up to 20 distinct stocks (shared 30-second cache), so P/L against entry can be discussed.
    sem = asyncio.Semaphore(4)

    async def price(sym: str):
        async with sem:
            return sym, await core.get_cmp(sym)
    prices = dict(await asyncio.gather(*(price(s) for s in list(dict.fromkeys(r["symbol"] for r in rows))[:20])))
    for r in rows:
        info = prices.get(r["symbol"])
        if info and info["cmp"] is not None and info["source"] in ("live", "last_trade"):
            r.update(cmp=round(info["cmp"], 2), cmp_source=info["source"], cmp_as_of=info["as_of"],
                     pct_vs_entry=round((info["cmp"] / r["entry"] - 1) * 100, 2) if r.get("entry") else None)
    return {"count": len(rows), "entries": rows,
            "note": "Where present, cmp is the current market price (live when the market is open). "
                    "This list is kept on the user's own computer and was shared by their browser with this question (newest 50 at most). "
                    "model is the rule set that produced the entry: trend (trend and momentum) or sr (support and resistance). Entries with no entry/stop/target had no LONG setup when added. Outcomes are on the Evaluation tab. "
                    "If count is 0 the list is empty or was not shared."}


def handlers_for(user_id: int, watchlist: Any = None) -> dict[str, Any]:
    """The tool handlers for one signed-in user: the shared ones plus their own watchlist."""
    return {**HANDLERS, "my_watchlist": functools.partial(my_watchlist, clean_watchlist(watchlist))}
