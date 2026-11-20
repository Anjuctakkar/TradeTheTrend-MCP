"""
Stock suggestions for the Portfolio Analysis tab.

Two steps, so the expensive part is done once and can be shared between users:

  1. scan()       Evaluate a wide universe of cash-market stocks: the top names by traded value (Bhavcopy
                  get_top_by_volume, asked for SCAN_SIZE = at least 250), topped up from the sector lists when that tool
                  returns fewer. Every stock is run through the chosen rule set(s) (trend and momentum,
                  support and resistance, or either); the ones with a LONG setup are kept with their levels and daily
                  returns. The server caches a scan for a few minutes.
  2. recommend()  Personalise a scan: drop stocks already held, rank by rule score and low correlation with the holdings,
                  check each against the current cash-market price (CM live feed) and skip setups already past their
                  stop or target, then split the budget equally across the top picks.

Mechanical screen only: not investment advice.
"""
import asyncio
import datetime as dt
import os
from typing import Any, Awaitable, Callable, Optional

import backtest
import sr_model
import technical

MODELS = ("trend", "sr", "either")         # which rule set(s) may produce a pick
MIN_SCAN = 250                              # the wide scan covers at least this many stocks
SCAN_SIZE = max(MIN_SCAN, int(os.getenv("SUGGEST_SCAN_SIZE", "250")))
QUICK_SIZE = 30                             # the fast option: only the 30 most traded
MAX_PICKS = 5
HISTORY_MONTHS = 6
CONCURRENCY = int(os.getenv("SUGGEST_SCAN_CONCURRENCY", "6"))


def size_choice(n: Any) -> int:
    """The two scan sizes on offer: QUICK_SIZE (30 most traded) or SCAN_SIZE (the wide scan, at least 250)."""
    try:
        return QUICK_SIZE if int(n) <= QUICK_SIZE else SCAN_SIZE
    except (TypeError, ValueError):
        return SCAN_SIZE


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


def technical_float(v: Any) -> Optional[float]:
    try:
        return None if v is None or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


def _traded_value(r: dict[str, Any]) -> Optional[float]:
    """The traded value (turnover) field of a get_top_by_volume row, whatever the server calls it."""
    for k, v in r.items():
        key = "".join(c for c in str(k).lower() if c.isalnum())
        if "turnover" in key or key == "value" or key.endswith("tradedvalue"):
            return technical_float(str(v).replace(",", "")) if isinstance(v, str) else technical_float(v)
    return None


async def universe(url: str, call_tool: Callable[..., Awaitable[Any]], extra: Any = (), size: Optional[int] = None) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, Any]]:
    """The stocks to scan: the top cash-market names by traded value, then `extra` (the sector lists) until there are `size` (30 or the wide size)."""
    size = size_choice(size) if size else SCAN_SIZE
    info: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    note: Optional[str] = None
    try:
        raw = await call_tool(url, "get_top_by_volume", {"date": dt.date.today().isoformat(), "n": size, "sortBy": "value"})
        if raw.get("is_error"):
            note = f"The top-by-traded-value list failed ({str(raw.get('note') or raw.get('content'))[:120]})."
        else:
            for r in find_records(raw.get("content")):
                sym = str(r.get("symbol") or "").strip().upper()
                if len(order) >= size:
                    break
                if sym and sym not in info:
                    info[sym] = {"source": "top_by_value", "traded_value": _traded_value(r)}
                    order.append(sym)
    except Exception as exc:  # noqa: BLE001 - fall back to the sector lists
        note = f"The top-by-traded-value list failed ({type(exc).__name__})."
    n_top = len(order)
    for sym in extra:
        if len(order) >= size:
            break
        sym = str(sym).strip().upper()
        if sym and sym not in info:
            info[sym] = {"source": "sector_lists"}
            order.append(sym)
    if n_top < size and not note:
        note = f"The top-by-traded-value list returned {n_top}; the rest came from the sector lists."
    return order, info, {"top_by_value": n_top, "from_sector_lists": len(order) - n_top, "total": len(order), "size": size, "note": note}


def _evaluate(sym: str, recs: list[dict[str, Any]], model: str) -> Optional[dict[str, Any]]:
    """One stock: the LONG setup from the chosen rule set(s), with its levels and daily returns, or None."""
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
    used, sig = max(found, key=lambda f: (int(f[1]["score"][0]), f[0] == "trend"))     # higher score wins; trend wins a tie
    return {"symbol": sym, "as_of": dates[-1][:10], "score": sig["score"], "entry": sig["entry"], "stop_loss": sig["stop_loss"],
            "target": sig["target"], "rsi": round(technical.latest_value(rsi) or 0, 1), "model": used,
            "setup": sig.get("setup", "trend_momentum"), "returns": _returns(dates, closes)}


async def scan(url: str, symbols: list[str], info: dict[str, dict[str, Any]], fetch_history: Callable[..., Awaitable[list[dict[str, Any]]]],
               model: str = "trend", progress: Optional[Callable[[int, int], None]] = None) -> dict[str, Any]:
    """Run every symbol through the rule set(s). progress(done, total) is called as stocks finish."""
    sem = asyncio.Semaphore(CONCURRENCY)
    total, state = len(symbols), {"done": 0, "failed": 0}

    async def one(sym: str) -> Optional[dict[str, Any]]:
        recs = None
        try:
            async with sem:
                recs = await fetch_history(url, sym, HISTORY_MONTHS)
        except Exception:  # noqa: BLE001 - one bad symbol must not stop the scan
            state["failed"] += 1
        out = None
        if recs:
            try:
                out = _evaluate(sym, recs, model)
            except Exception:  # noqa: BLE001
                state["failed"] += 1
        elif recs is not None:
            state["failed"] += 1
        state["done"] += 1
        if progress:
            progress(state["done"], total)
        return out

    results = await asyncio.gather(*(one(s) for s in symbols))
    longs = [r for r in results if r]
    for r in longs:
        r.update({k: v for k, v in info.get(r["symbol"], {}).items()})
    return {"model": model, "scanned": total, "evaluated": total - state["failed"], "failed": state["failed"], "longs": longs,
            "scanned_at": dt.datetime.now().isoformat(timespec="seconds")}


async def recommend(
    url: str,
    existing: list[dict[str, Any]],
    budget: float,
    fetch_history: Callable[..., Awaitable[list[dict[str, Any]]]],
    call_tool: Callable[..., Awaitable[Any]],
    price_lookup: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None,
    model: str = "trend",
    scan_result: Optional[dict[str, Any]] = None,
    extra_symbols: Any = (),
    scan_size: Optional[int] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """Personalise a scan (passed in, or run now). price_lookup(symbol, eod_close, eod_date) -> {cmp, source, as_of, ...} supplies the
    current cash-market price: setups already below their stop or above their target at that price are skipped, and quantities use it."""
    if model not in MODELS:
        raise ValueError(f"model must be one of {MODELS}")
    if scan_result is None:
        symbols, info, uni = await universe(url, call_tool, extra_symbols, scan_size)
        if not symbols:
            return {"budget": budget, "scanned": 0, "longs_found": 0, "picks": [], "model": model, "note": uni.get("note") or "No stocks to scan."}
        scan_result = await scan(url, symbols, info, fetch_history, model, progress)
        scan_result["universe"] = uni
    held = {str(e.get("symbol", "")).upper() for e in existing}
    port_ret = _portfolio_returns(existing)
    longs: list[dict[str, Any]] = []
    for l in scan_result["longs"]:
        if l["symbol"] in held:
            continue
        corr = _corr(l["returns"], port_ret) if port_ret else None
        longs.append({**{k: v for k, v in l.items() if k != "returns"}, "correlation": None if corr is None else round(corr, 2)})
    # Higher score first, then lowest correlation with the current portfolio.
    longs.sort(key=lambda e: (-int(e["score"][0]), 2 if e["correlation"] is None else e["correlation"]))
    skipped: list[dict[str, Any]] = []
    if price_lookup is None:
        picks = longs[:MAX_PICKS]
    else:
        pool = longs[:MAX_PICKS * 2]        # a few more than needed: some may already be past their stop or target at today's price
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
    return {"budget": budget, "scanned": scan_result["scanned"], "evaluated": scan_result.get("evaluated"), "failed": scan_result.get("failed", 0),
            "longs_found": len(longs), "picks": picks, "skipped_by_price": skipped, "model": model,
            "universe": scan_result.get("universe"), "scanned_at": scan_result.get("scanned_at")}
