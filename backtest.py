"""
Backtest and forward evaluation of the rule-based LONG setups.

Backtest: walk forward through a price history. On each bar, compute the
signal using ONLY data up to that bar (indicators are causal, so slicing the
full series is identical). If it is LONG, enter at that bar's close (the same
"entry = last close" the app suggests), then step through later bars:

  * open <= stop   -> exit at the open (gap down)
  * open >= target -> exit at the open (gap up)
  * low  <= stop   -> exit at the stop   (checked first if a bar hits both,
                                          the conservative assumption)
  * high >= target -> exit at the target
  * held max_hold bars -> exit at the close (time stop)

No overlapping trades per symbol. A round-trip cost (brokerage + slippage,
percent) is deducted from every trade. Simplified: no position sizing,
taxes, or partial fills.
"""
from typing import Any, Optional

import technical

WARMUP = 50  # bars needed before SMA50 exists


def _f(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def prepare(records: list[dict[str, Any]]) -> dict[str, list]:
    """Records -> parallel lists. Rows without a close are dropped; missing
    open/high/low fall back to the close."""
    dates, o, h, l, c = [], [], [], [], []
    for r in records:
        r = {str(k).lower(): v for k, v in r.items()}
        close = _f(r.get("close"))
        if close is None:
            continue
        dates.append(str(r.get("date", ""))[:10])
        c.append(close)
        o.append(_f(r.get("open")) or close)
        h.append(_f(r.get("high")) or close)
        l.append(_f(r.get("low")) or close)
    return {"dates": dates, "o": o, "h": h, "l": l, "c": c}


def simulate_exit(o, h, l, c, start: int, stop: float, target: float, max_hold: int):
    """Step bars after `start`. Returns (exit_idx, exit_price, reason);
    reason 'open' means the data ended before any exit."""
    n = len(c)
    last = min(start + max_hold, n - 1)
    for j in range(start + 1, last + 1):
        if o[j] <= stop:
            return j, o[j], "stop"
        if o[j] >= target:
            return j, o[j], "target"
        if l[j] <= stop:
            return j, stop, "stop"
        if h[j] >= target:
            return j, target, "target"
        if j - start == max_hold:
            return j, c[j], "time"
    return n - 1, c[n - 1], "open"


def run(records: list[dict[str, Any]], max_hold: int = 20, cost_pct: float = 0.2) -> dict[str, Any]:
    p = prepare(records)
    dates, o, h, l, c = p["dates"], p["o"], p["h"], p["l"], p["c"]
    n = len(c)
    if n < WARMUP + 10:
        raise ValueError(f"Only {n} bars of history; need at least {WARMUP + 10} to backtest.")

    s20, s50 = technical.sma(c, 20), technical.sma(c, 50)
    rsi = technical.rsi(c, 14)
    ml, ms = technical.macd(c)

    trades: list[dict[str, Any]] = []
    i = WARMUP - 1
    while i < n - 1:
        sig = technical.generate_trade_signal(c[: i + 1], s20[: i + 1], s50[: i + 1],
                                              rsi[: i + 1], ml[: i + 1], ms[: i + 1])
        if sig.get("signal") != "LONG":
            i += 1
            continue
        entry, stop, target = sig["entry"], sig["stop_loss"], sig["target"]
        j, px, reason = simulate_exit(o, h, l, c, i, stop, target, max_hold)
        trades.append({
            "entry_idx": i, "exit_idx": j,
            "entry_date": dates[i], "exit_date": dates[j],
            "entry": entry, "stop": stop, "target": target,
            "exit": round(px, 2), "reason": reason, "bars": j - i,
            "score": sig["score"],
            "ret_pct": round((px / entry - 1) * 100 - cost_pct, 2),
        })
        if reason == "open":
            break
        i = j + 1

    # Daily mark-to-market equity (flat when out of the market) vs buy & hold.
    first = WARMUP - 1
    equity, bh = [1.0], [1.0]
    by_entry = {t["entry_idx"]: t for t in trades}
    active: Optional[dict[str, Any]] = None
    in_market = 0
    for t in range(first + 1, n):
        e = equity[-1]
        if active is None and (t - 1) in by_entry:
            active = by_entry[t - 1]
        if active is not None:
            price = active["exit"] if t == active["exit_idx"] else c[t]
            e *= price / c[t - 1]
            in_market += 1
            if t == active["exit_idx"]:
                e *= 1 - cost_pct / 100
                active = None
        equity.append(e)
        bh.append(c[t] / c[first])

    peak, max_dd = 1.0, 0.0
    for e in equity:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak)

    closed = [t for t in trades if t["reason"] != "open"]
    wins = [t["ret_pct"] for t in closed if t["ret_pct"] > 0]
    losses = [t["ret_pct"] for t in closed if t["ret_pct"] <= 0]
    metrics = {
        "trades": len(closed),
        "open_trades": len(trades) - len(closed),
        "win_rate": round(100 * len(wins) / len(closed), 1) if closed else None,
        "avg_return_pct": round(sum(t["ret_pct"] for t in closed) / len(closed), 2) if closed else None,
        "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss_pct": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None,
        "strategy_return_pct": round((equity[-1] - 1) * 100, 1),
        "buy_hold_return_pct": round((bh[-1] - 1) * 100, 1),
        "max_drawdown_pct": round(max_dd * 100, 1),
        "exposure_pct": round(100 * in_market / (n - 1 - first), 1),
        "tested_bars": n - first,
        "target_hits": sum(1 for t in closed if t["reason"] == "target"),
        "stop_hits": sum(1 for t in closed if t["reason"] == "stop"),
        "time_exits": sum(1 for t in closed if t["reason"] == "time"),
    }
    for t in trades:
        del t["entry_idx"], t["exit_idx"]
    return {"metrics": metrics, "trades": trades,
            "dates": dates[first:], "equity": [round(x, 4) for x in equity],
            "buy_hold": [round(x, 4) for x in bh]}


def pool(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate across symbols: pooled trade stats + average symbol returns."""
    trades = [t for r in results for t in r["trades"] if t["reason"] != "open"]
    wins = [t["ret_pct"] for t in trades if t["ret_pct"] > 0]
    losses = [t["ret_pct"] for t in trades if t["ret_pct"] <= 0]
    k = len(results)
    return {
        "symbols": k,
        "trades": len(trades),
        "win_rate": round(100 * len(wins) / len(trades), 1) if trades else None,
        "avg_return_pct": round(sum(t["ret_pct"] for t in trades) / len(trades), 2) if trades else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None,
        "avg_strategy_return_pct": round(sum(r["metrics"]["strategy_return_pct"] for r in results) / k, 1) if k else None,
        "avg_buy_hold_return_pct": round(sum(r["metrics"]["buy_hold_return_pct"] for r in results) / k, 1) if k else None,
        "small_sample": len(trades) < 30,
    }


def evaluate_forward(records: list[dict[str, Any]], as_of: str, entry: float, stop: float,
                     target: float, max_hold: int = 20, cost_pct: float = 0.2) -> dict[str, Any]:
    """How a logged suggestion has played out since `as_of` (real, out-of-sample)."""
    p = prepare(records)
    dates, o, h, l, c = p["dates"], p["o"], p["h"], p["l"], p["c"]
    start = max((i for i, d in enumerate(dates) if d <= as_of), default=None)
    if start is None:
        return {"status": "no_data"}
    # If a split/bonus was applied to the history, rescale the logged levels
    # onto the same basis as the adjusted bar the suggestion was made on.
    scale = c[start] / entry if entry else 1.0
    stop, target, entry = stop * scale, target * scale, c[start]
    if start >= len(c) - 1:
        return {"status": "pending", "current_price": round(c[-1], 2), "return_pct": 0.0, "bars": 0}
    j, px, reason = simulate_exit(o, h, l, c, start, stop, target, max_hold)
    ret = (px / entry - 1) * 100
    if reason == "open":
        return {"status": "open", "current_price": round(px, 2), "return_pct": round(ret, 2), "bars": j - start}
    return {"status": {"target": "target", "stop": "stopped", "time": "expired"}[reason],
            "exit_date": dates[j], "exit_price": round(px / scale, 2),
            "return_pct": round(ret - cost_pct, 2), "bars": j - start}


def summarize_forward(rows: list[dict[str, Any]]) -> dict[str, Any]:
    resolved = [r for r in rows if r["status"] in ("target", "stopped", "expired")]
    wins = [r for r in resolved if r["return_pct"] > 0]
    live = [r for r in rows if r["status"] in ("open", "pending")]
    return {
        "logged": len(rows), "resolved": len(resolved), "open": len(live),
        "target": sum(1 for r in resolved if r["status"] == "target"),
        "stopped": sum(1 for r in resolved if r["status"] == "stopped"),
        "expired": sum(1 for r in resolved if r["status"] == "expired"),
        "win_rate": round(100 * len(wins) / len(resolved), 1) if resolved else None,
        "avg_return_pct": round(sum(r["return_pct"] for r in resolved) / len(resolved), 2) if resolved else None,
        "open_avg_return_pct": round(sum(r["return_pct"] for r in live) / len(live), 2) if live else None,
        "small_sample": len(resolved) < 30,
    }
