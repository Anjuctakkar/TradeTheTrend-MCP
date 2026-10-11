"""
52-week range and a proposed holding period, each with its reasoning.

The holding period is only proposed when the evidence supports one. It is
"no proposal" (with the reason) when there is no LONG setup, when the stop and
target are too wide for the stock's usual movement, when there isn't enough
history, or when the two estimates below disagree.

How the proposal is built (trading days):

  1. Volatility estimate. For a random walk with daily move `s`, the expected
     time to hit either a stop `a` below or a target `b` above the entry is
     a * b / s^2 (a and b as fractions of the entry price; `s` is the standard
     deviation of the last 60 daily returns). This is the time the setup
     typically needs to resolve, given how far away its exits are and how
     much the stock normally moves.
  2. History cross-check. The same rule set is replayed on this stock's own
     past prices (backtest.run). If it made at least 5 closed trades, their
     median length is compared with step 1.
  3. If the two are within 2x of each other, the proposal is their average.
     If they differ by more than 2x, nothing is proposed. With too few
     trades to cross-check, the volatility estimate is used alone and the
     proposal is marked lower confidence.
  4. If the estimate is longer than 40 trading days (about two months), the
     exits are too far away for a holding period to mean much: no proposal.

It is a "check back by" date, not a prediction: the trade can end sooner by
reaching the stop or the target.
"""
import datetime as dt
import statistics
from typing import Any, Optional

import backtest

HOLD_CAP = 40        # trading days; longer than this -> no proposal
MIN_TRADES = 5       # closed trades needed to cross-check against history
VOL_WINDOW = 60      # recent daily returns used for the volatility estimate
AGREE_RATIO = 2.0    # the two estimates must be within this factor


def _add_business_days(date_str: str, n: int) -> str:
    d, added = dt.date.fromisoformat(date_str[:10]), 0
    while added < n:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d.isoformat()


def week52(records: list[dict[str, Any]]) -> dict[str, Any]:
    """52-week high/low from (split-adjusted) daily bars, using intraday highs and
    lows when the data has them, otherwise closes."""
    p = backtest.prepare(records)
    dates, h, l, c = p["dates"], p["h"], p["l"], p["c"]
    if not dates:
        return {"status": "no_data"}
    has_hl = any("high" in {str(k).lower() for k in r} and "low" in {str(k).lower() for k in r} for r in records)
    last = dt.date.fromisoformat(dates[-1])
    start = (last - dt.timedelta(days=364)).isoformat()
    idx = [i for i, d in enumerate(dates) if d >= start]
    hi_i = max(idx, key=lambda i: h[i])
    lo_i = min(idx, key=lambda i: l[i])
    high, low, price = h[hi_i], l[lo_i], c[-1]
    covered = (last - dt.date.fromisoformat(dates[idx[0]])).days
    return {
        "status": "ok",
        "high": round(high, 2), "high_date": dates[hi_i],
        "low": round(low, 2), "low_date": dates[lo_i],
        "price": round(price, 2), "price_date": dates[-1],
        "pct_below_high": round((high - price) / high * 100, 1) if high else None,
        "pct_above_low": round((price - low) / low * 100, 1) if low else None,
        "position_pct": round((price - low) / (high - low) * 100) if high > low else None,
        "basis": "intraday highs and lows" if has_hl else "closing prices",
        "days_covered": covered, "partial": covered < 350,
    }


def propose(signal: dict[str, Any], records: list[dict[str, Any]], w52: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """signal: a generate_trade_signal() result (needs signal/entry/stop_loss/target for LONG)."""
    out: dict[str, Any] = {"status": "none", "days": None, "review_by": None, "confidence": None,
                           "reasoning": [], "evidence": {}}
    why = out["reasoning"]

    if signal.get("signal") != "LONG":
        why.append("There is no LONG setup, so there is no entry, stop or target to time. No holding period is proposed.")
        return out

    entry, stop, target = signal["entry"], signal["stop_loss"], signal["target"]
    p = backtest.prepare(records)
    c, dates = p["c"], p["dates"]
    rets = [c[i] / c[i - 1] - 1 for i in range(1, len(c)) if c[i - 1]]
    window = rets[-VOL_WINDOW:]
    if len(window) < 20:
        why.append("There isn't enough price history to measure how much the stock moves in a day. No holding period is proposed.")
        return out
    sigma = statistics.pstdev(window)
    a, b = (entry - stop) / entry, (target - entry) / entry
    if sigma <= 0 or a <= 0 or b <= 0:
        why.append("The stop, target or daily movement couldn't be measured sensibly. No holding period is proposed.")
        return out

    model = a * b / sigma ** 2
    out["evidence"]["model_days"] = round(model, 1)
    out["evidence"]["daily_move_pct"] = round(sigma * 100, 2)
    why.append(f"The stop is {a * 100:.1f}% below the entry and the target {b * 100:.1f}% above it. Over the last {len(window)} trading days the stock moved about {sigma * 100:.1f}% a day.")
    why.append(f"At that pace, price typically needs about {model:.0f} trading days to reach either the stop or the target (estimate: stop distance x target distance / daily variance).")

    if w52 and w52.get("status") == "ok":
        if target > w52["high"]:
            why.append(f"The target (₹{target}) is above the 52-week high (₹{w52['high']}), so reaching it needs a new 52-week high.")
        else:
            why.append(f"The target (₹{target}) is {(w52['high'] - target) / w52['high'] * 100:.1f}% below the 52-week high (₹{w52['high']}).")

    if model > HOLD_CAP:
        why.append(f"That is longer than {HOLD_CAP} trading days (about two months). The stop and target are far apart for how this stock normally moves, so any holding period would be a guess. None is proposed.")
        return out

    try:
        bt = backtest.run(records, HOLD_CAP, 0.2)
        closed = [t for t in bt["trades"] if t["reason"] != "open"]
    except ValueError:
        closed = []
    n = len(closed)
    out["evidence"]["history_trades"] = n

    if n < MIN_TRADES:
        days = max(1, round(model))
        out["confidence"] = "lower"
        why.append(f"On this stock's own past prices the same rules made only {n} closed trade{'s' if n != 1 else ''}, too few to cross-check (at least {MIN_TRADES} needed). The proposal rests on the volatility estimate alone, so treat it as lower confidence.")
    else:
        emp = statistics.median(t["bars"] for t in closed)
        hits = [t["bars"] for t in closed if t["reason"] == "target"]
        stops = sum(1 for t in closed if t["reason"] == "stop")
        out["evidence"].update({"history_median_days": emp, "history_target_hits": len(hits), "history_stops": stops})
        why.append(f"Cross-check: on this stock's past prices the same rules made {n} closed trades; the median lasted {emp:.0f} trading days ({len(hits)} reached the target, {stops} hit the stop, {n - len(hits) - stops} ran out of time).")
        ratio = max(model, emp) / max(min(model, emp), 0.5)
        if ratio > AGREE_RATIO:
            why.append(f"The two estimates ({model:.0f} and {emp:.0f} days) differ by more than {AGREE_RATIO:.0f}x, so they don't support one number. No holding period is proposed.")
            return out
        days = max(1, round((model + emp) / 2))
        out["confidence"] = "higher"
        why.append(f"The two estimates agree within {AGREE_RATIO:.0f}x, so the proposal is their average.")

    out.update(status="proposed", days=days, review_by=_add_business_days(dates[-1], days))
    why.append(f"Proposal: check back after about {days} trading days (around {out['review_by']}, ignoring market holidays, counted from the last price date). The trade can end sooner at the stop or the target. This is not a prediction.")
    return out
