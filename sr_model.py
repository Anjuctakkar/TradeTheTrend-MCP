"""
Support / resistance rule-based long setup.

A second mechanical model next to technical.generate_trade_signal(). Same output shape
(signal, score "N/4", reasons, entry, stop_loss, target, risk_per_share, reward_risk_ratio),
so recommend.py, holding.py, backtest.py and technical.check_price() work with it unchanged.
Long setups only; defaults to NO_TRADE. A rule-based heuristic, NOT investment advice.

How the levels are found (causal, no look-ahead)
  1. Swing pivots: a bar is a pivot high if its high is the highest of the PIVOT_K bars either side
     (pivot low: the lowest low). A pivot is only known PIVOT_K bars after it happens, so the last
     PIVOT_K bars never contribute. That is what keeps backtests honest.
  2. Zones: pivot highs and lows of the last LOOKBACK bars are pooled and merged when they sit within
     CLUSTER_ATR x ATR of each other. A zone's touches = the number of pivots in it. A zone needs at
     least MIN_TOUCHES. Pooling both kinds lets a broken resistance turn into support (role reversal).
  3. A zone below the current price is support, above it resistance.

Two setups (the one with the better reward:risk is used)
  A. Support bounce      The last bar dipped into a support zone, closed back above the level, closed up
                         (close > open) and finished in the upper half of its range.
  B. Resistance breakout The last close is above a resistance zone by at least BREAKOUT_BUFFER x ATR,
                         the closes of the 3 bars before it were not already above the zone (a fresh
                         breakout), and the bar closed in the upper 40% of its range.

Stop / target
  Entry  = last close.
  Stop   = below the zone by STOP_BUFFER x ATR (and below the signal bar's low).
  Target = just under the nearest resistance above the entry. With no resistance overhead, 2R.

Score (1 point each, out of 4): zone has 3+ touches; trigger fired (always 1 for a setup);
close above the 50-day SMA; reward:risk of at least 2.
Vetoes to NO_TRADE: no trigger, risk wider than MAX_RISK_ATR x ATR, reward:risk below MIN_RR,
fewer than 3/4 points, not enough history.
"""
from typing import Any, Optional

PIVOT_K = 5            # bars either side that define a swing pivot
LOOKBACK = 120         # bars of history used to build zones
ATR_N = 14
CLUSTER_ATR = 0.75     # pivots within this many ATRs merge into one zone
ZONE_ATR = 0.5         # half-width of a zone, in ATRs
MIN_TOUCHES = 2
STOP_BUFFER_ATR = 0.5
BREAKOUT_BUFFER_ATR = 0.25
MAX_RISK_ATR = 3.0
MIN_RR = 1.5
GOOD_RR = 2.0
MIN_BARS = 60


def _atr(h: list[float], l: list[float], c: list[float], n: int = ATR_N) -> Optional[float]:
    """Simple average of the last n true ranges (same plain-average style as the project's RSI)."""
    if len(c) < n + 1:
        return None
    tr = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(len(c) - n, len(c))]
    return sum(tr) / n


def _sma_last(c: list[float], n: int) -> Optional[float]:
    return sum(c[-n:]) / n if len(c) >= n else None


def find_pivots(h: list[float], l: list[float], k: int = PIVOT_K, lookback: int = LOOKBACK) -> list[tuple[int, float, str]]:
    """Confirmed swing pivots as (index, price, 'H' or 'L'). Needs k bars after the pivot, so the newest k bars are never pivots."""
    n = len(h)
    out: list[tuple[int, float, str]] = []
    for p in range(max(k, n - lookback), n - k):
        if h[p] > max(h[p - k:p]) and h[p] >= max(h[p + 1:p + k + 1]):
            out.append((p, h[p], "H"))
        if l[p] < min(l[p - k:p]) and l[p] <= min(l[p + 1:p + k + 1]):
            out.append((p, l[p], "L"))
    return out


def build_zones(pivots: list[tuple[int, float, str]], cluster_tol: float, min_touches: int = MIN_TOUCHES) -> list[dict[str, Any]]:
    """Merge nearby pivot prices into zones: {level, touches, last_idx}, sorted by level."""
    zones: list[dict[str, Any]] = []
    for idx, price, _ in sorted(pivots, key=lambda x: x[1]):
        if zones and price - (sum(zones[-1]["prices"]) / len(zones[-1]["prices"])) <= cluster_tol:
            zones[-1]["prices"].append(price)
            zones[-1]["idxs"].append(idx)
        else:
            zones.append({"prices": [price], "idxs": [idx]})
    return [{"level": sum(z["prices"]) / len(z["prices"]), "touches": len(z["prices"]), "last_idx": max(z["idxs"])}
            for z in zones if len(z["prices"]) >= min_touches]


def _no_trade(reasons: list[str], score: Optional[str] = None, levels: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    out: dict[str, Any] = {"signal": "NO_TRADE", "score": score, "reasons": reasons, "model": "sr"}
    if levels:
        out["levels"] = levels
    return out


def generate_sr_signal(o: list[float], h: list[float], l: list[float], c: list[float]) -> dict[str, Any]:
    """Signal for the LAST bar of the given (split-adjusted) OHLC lists."""
    n = len(c)
    if n < MIN_BARS:
        return _no_trade([f"Not enough price history ({n} bars; need {MIN_BARS}+) to find support and resistance."])
    a = _atr(h, l, c)
    if not a or a <= 0:
        return _no_trade(["Average true range couldn't be measured."])

    price = c[-1]
    zones = build_zones(find_pivots(h, l), CLUSTER_ATR * a)
    support = [z for z in zones if z["level"] < price]
    resist = [z for z in zones if z["level"] > price]
    levels = {
        "atr": round(a, 2),
        "support": [{"level": round(z["level"], 2), "touches": z["touches"]} for z in sorted(support, key=lambda z: -z["level"])[:3]],
        "resistance": [{"level": round(z["level"], 2), "touches": z["touches"]} for z in sorted(resist, key=lambda z: z["level"])[:3]],
    }
    if not zones:
        return _no_trade(["No support or resistance zone with at least 2 touches in the last 120 bars."], levels=levels)

    tol = ZONE_ATR * a
    rng = max(h[-1] - l[-1], 1e-9)
    candidates: list[dict[str, Any]] = []

    # A. Support bounce
    bounce_ok = c[-1] > o[-1] and (c[-1] - l[-1]) >= 0.5 * rng
    if bounce_ok:
        hits = [z for z in zones if l[-1] <= z["level"] + tol and c[-1] > z["level"] and l[-1] >= z["level"] - 3 * a]
        if hits:
            z = max(hits, key=lambda z: z["level"])
            candidates.append({"setup": "support_bounce", "zone": z,
                               "why": f"Bar dipped to the support zone near {z['level']:.2f} ({z['touches']} touches), closed back above it and closed up in the top half of its range"})

    # B. Resistance breakout
    if n >= 5 and (c[-1] - l[-1]) >= 0.6 * rng:
        hits = [z for z in zones if c[-1] > z["level"] + BREAKOUT_BUFFER_ATR * a and max(c[-4:-1]) <= z["level"] + tol]
        if hits:
            z = max(hits, key=lambda z: z["level"])
            candidates.append({"setup": "resistance_breakout", "zone": z,
                               "why": f"Fresh close above the resistance zone near {z['level']:.2f} ({z['touches']} touches), in the top 40% of the bar's range"})

    if not candidates:
        near_s = f"nearest support {levels['support'][0]['level']}" if levels["support"] else "no support below"
        near_r = f"nearest resistance {levels['resistance'][0]['level']}" if levels["resistance"] else "no resistance above"
        return _no_trade([f"No trigger: price is not bouncing off support or breaking out of resistance ({near_s}, {near_r})."], levels=levels)

    sma50 = _sma_last(c, 50)
    best: Optional[dict[str, Any]] = None
    veto_reasons: list[str] = []
    for cand in candidates:
        z = cand["zone"]
        stop = min(z["level"] - STOP_BUFFER_ATR * a, l[-1] - 0.1 * a)
        risk = price - stop
        if risk <= 0:
            continue
        if risk > MAX_RISK_ATR * a:
            veto_reasons.append(f"{cand['setup'].replace('_', ' ')}: stop would be {risk / a:.1f} ATR away (limit {MAX_RISK_ATR:g}), too wide")
            continue
        above = [r for r in resist if r["level"] > price + 0.5 * a and r["level"] != z["level"]]
        if above:
            nxt = min(above, key=lambda r: r["level"])
            target, tnote = nxt["level"] - 0.1 * a, f"target just under resistance near {nxt['level']:.2f}"
        else:
            target, tnote = price + 2 * risk, "no resistance overhead, so target is 2x the risk"
        rr = (target - price) / risk
        if rr < MIN_RR:
            veto_reasons.append(f"{cand['setup'].replace('_', ' ')}: reward:risk {rr:.2f} is below {MIN_RR:g}, resistance is too close")
            continue
        pts = [z["touches"] >= 3, True, sma50 is not None and price > sma50, rr >= GOOD_RR]
        res = {**cand, "stop": stop, "risk": risk, "target": target, "rr": rr, "tnote": tnote, "pts": pts}
        if best is None or rr > best["rr"]:
            best = res

    if best is None:
        return _no_trade(veto_reasons or ["Setup rejected on risk."], levels=levels)

    pts = best["pts"]
    points = sum(pts)
    reasons = [
        best["why"],
        f"Zone quality: {best['zone']['touches']} touches ({'3+ earns a point' if pts[0] else 'under 3, no point'})",
        f"Trend: close {'above' if pts[2] else 'not above'} the 50-day SMA" + (f" ({sma50:.2f})" if sma50 else ""),
        f"Stop {best['risk'] / a:.1f} ATR below entry; {best['tnote']}; reward:risk {best['rr']:.2f}",
    ]
    if points < 3:
        return _no_trade(reasons, score=f"{points}/4", levels=levels)

    return {
        "signal": "LONG", "model": "sr", "score": f"{points}/4", "setup": best["setup"], "reasons": reasons, "levels": levels,
        "entry": round(price, 2), "stop_loss": round(best["stop"], 2), "target": round(best["target"], 2),
        "risk_per_share": round(best["risk"], 2), "reward_risk_ratio": round(best["rr"], 2),
    }


def signal_from_prepared(p: dict[str, list], i: int) -> dict[str, Any]:
    """Adapter for backtest.run(signal_fn=...): the signal as of bar i, using only bars up to i."""
    s = max(0, i + 1 - (LOOKBACK + 60))
    return generate_sr_signal(p["o"][s:i + 1], p["h"][s:i + 1], p["l"][s:i + 1], p["c"][s:i + 1])
