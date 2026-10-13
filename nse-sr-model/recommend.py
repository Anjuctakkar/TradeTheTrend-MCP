"""
Stock suggestions for the Portfolio Analysis tab.

Scan the most-traded NSE stocks, keep the ones technical.generate_trade_signal() (trend and
momentum) and/or sr_model.generate_sr_signal() (support and resistance) flag as LONG, rank by rule score and low correlation with the existing
holdings, then split the budget equally across the top picks.
Mechanical screen only: not investment advice.
"""
import asyncio
import datetime as dt
from typing import Any, Awaitable, Callable, Optional

import backtest
import sr_model
import technical

MODELS = ("trend", "sr", "either")   # which rule set(s) may produce a pick
SCAN_SIZE = 30      # most-traded names to scan
MAX_PICKS = 5
HISTORY_MONTHS = 6
CONCURRENCY = 4


def find_records(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list) and content and isinstance(content[0], dict):
        return content
    if isinstance(content, dict):
        for v in content.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def _returns(dates: list[str], closes: list[Optional[float]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for i in range(1, len(closes)):
        a, b = closes[i - 1], closes[i]
        if a and b:
            out[dates[i][:10]] = b / a - 1
    return out


def _corr(x: dict[str, float], y: dict[str, float]) -> Optional[float]:
    keys = sorted(set(x) & set(y))
    if len(keys) < 20:
        return None
    xs, ys = [x[k] for k in keys], [y[k] for k in keys]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((a - mx) ** 2 for a in xs)
    syy = sum((b - my) ** 2 for b in ys)
    if sxx == 0 or syy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / (sxx * syy) ** 0.5


def _portfolio_returns(existing: list[dict[str, Any]]) -> dict[str, float]:
    per = [_returns([str(d) for d in e.get("dates", [])], e.get("close", [])) for e in existing]
    per = [p for p in per if p]
    days = sorted(set().union(*per)) if per else []
    return {d: sum(p[d] for p in per if d in p) / sum(1 for p in per if d in p) for d in days}


async def recommend(
    url: str,
    existing: list[dict[str, Any]],
    budget: float,
    fetch_history: Callable[..., Awaitable[list[dict[str, Any]]]],
    call_tool: Callable[..., Awaitable[Any]],
    price_lookup: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None,
    model: str = "trend",
) -> dict[str, Any]:
    """price_lookup(symbol, eod_close, eod_date) -> {cmp, source, as_of, ...} supplies the current market price. With it,
    setups already below their stop or above their target at that price are skipped, and quantities use the current price."""
    if model not in MODELS:
        raise ValueError(f"model must be one of {MODELS}")
    held = {str(e.get("symbol", "")).upper() for e in existing}
    today = dt.date.today().isoformat()

    raw = await call_tool(url, "get_top_by_volume", {"date": today, "n": SCAN_SIZE, "sortBy": "value"})
    if raw.get("is_error"):
        raise RuntimeError(f"get_top_by_volume failed: {raw.get('note') or raw.get('content')}")
    candidates = [
        str(r.get("symbol")).upper() for r in find_records(raw.get("content")) if r.get("symbol")
    ]
    candidates = [s for s in dict.fromkeys(candidates) if s not in held]
    if not candidates:
        return {"budget": budget, "scanned": 0, "longs_found": 0, "picks": [],
                "note": "No candidates returned by get_top_by_volume."}

    port_ret = _portfolio_returns(existing)
    sem = asyncio.Semaphore(CONCURRENCY)

    async def evaluate(sym: str) -> Optional[dict[str, Any]]:
        async with sem:
            try:
                recs = await fetch_history(url, sym, HISTORY_MONTHS)
            except Exception:  # noqa: BLE001
                return None
        if not recs:
            return None
        dates = [str(r.get("date", "")) for r in recs]
        closes = [technical_float(r.get("close")) for r in recs]
        macd_l, macd_s = technical.macd(closes)
        s20, s50, rsi = technical.sma(closes, 20), technical.sma(closes, 50), technical.rsi(closes, 14)
        found: list[tuple[str, dict[str, Any]]] = []
        if model in ("trend", "either"):
            t_sig = technical.generate_trade_signal(closes, s20, s50, rsi, macd_l, macd_s)
            if t_sig.get("signal") == "LONG":
                found.append(("trend", t_sig))
        if model in ("sr", "either"):
            p = backtest.prepare(recs)
            s_sig = sr_model.generate_sr_signal(p["o"], p["h"], p["l"], p["c"])
            if s_sig.get("signal") == "LONG":
                found.append(("sr", s_sig))
        if not found:
            return None
        # If both rule sets fire, keep the one with the higher score (the trend rules win a tie).
        used, sig = max(found, key=lambda f: (int(f[1]["score"][0]), f[0] == "trend"))
        corr = _corr(_returns(dates, closes), port_ret) if port_ret else None
        return {"symbol": sym, "as_of": dates[-1][:10], "score": sig["score"], "entry": sig["entry"],
                "stop_loss": sig["stop_loss"], "target": sig["target"],
                "rsi": round(technical.latest_value(rsi) or 0, 1),
                "model": used, "setup": sig.get("setup", "trend_momentum"),
                "correlation": None if corr is None else round(corr, 2)}

    evaluated = await asyncio.gather(*(evaluate(s) for s in candidates))
    longs = [e for e in evaluated if e]
    # Higher score first, then lowest correlation with the current portfolio.
    longs.sort(key=lambda e: (-int(e["score"][0]), 2 if e["correlation"] is None else e["correlation"]))
    skipped: list[dict[str, Any]] = []
    if price_lookup is None:
        picks = longs[:MAX_PICKS]
    else:
        # Look at a few more than we need: a setup that is already past its stop or target at today's price is skipped.
        pool = longs[:MAX_PICKS * 2]
        infos = await asyncio.gather(*(price_lookup(p["symbol"], p["entry"], p["as_of"]) for p in pool), return_exceptions=True)
        picks = []
        for p, info in zip(pool, infos):
            if isinstance(info, BaseException) or not isinstance(info, dict) or info.get("cmp") is None:
                info = {"cmp": None, "source": "none", "as_of": None, "change_pct": None, "note": "Current price unavailable."}
            p.update({"cmp": info["cmp"], "cmp_source": info.get("source"), "cmp_as_of": info.get("as_of"),
                      "cmp_change_pct": info.get("change_pct"), "cmp_note": info.get("note")})
            p["cmp_check"] = technical.check_price(p["entry"], p["stop_loss"], p["target"], info["cmp"]) if info["cmp"] is not None else None
            if p["cmp_check"] and p["cmp_check"]["status"] != "ok":
                skipped.append({"symbol": p["symbol"], "cmp": info["cmp"], "entry": p["entry"],
                                "reason": "price is already below its stop" if p["cmp_check"]["status"] == "below_stop" else "price has already reached its target"})
                continue
            picks.append(p)
        picks = picks[:MAX_PICKS]

    each = budget / len(picks) if picks else 0
    for p in picks:
        buy = p.get("cmp") or p["entry"]                 # what you would pay now, not last night's close
        p["quantity"] = int(each // buy) if buy else 0
        p["invested"] = round(p["quantity"] * buy, 2)
    return {"budget": budget, "scanned": len(candidates), "longs_found": len(longs), "picks": picks, "skipped_by_price": skipped, "model": model}


def technical_float(v: Any) -> Optional[float]:
    try:
        return None if v is None or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None
