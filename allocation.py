"""
Capital allocation across the stocks in a portfolio.

"Suggested" mode is a transparent, mechanical rule, not advice:

  1. Only stocks with a LONG setup get capital. A stock with No trade gets
     nothing.
  2. Risk balance: among the LONG stocks, each raw weight is 1 / annualised
     volatility, so a calmer stock gets more and a jumpier one gets less.
     Volatility is the standard deviation of daily returns over the history,
     times sqrt(252).
  3. Cap: no stock gets more than max_weight_pct (the excess is shared among
     the other LONG stocks).

If the cap stops the LONG stocks from adding up to 100% (for example one LONG
stock with a 35% cap), the rest is NOT forced into them: it is reported as
unallocated cash. Raise the cap or add stocks to deploy more. If no stock has
a LONG setup, everything is unallocated.

Quantities are whole shares, so a small amount is left over per stock.
"""
import math
import statistics
from typing import Any, Optional

MIN_RETURNS = 20  # need this many daily returns to trust a volatility figure


def annual_vol(closes: list[Optional[float]]) -> Optional[float]:
    """Annualised volatility as a fraction (0.25 = 25%), or None if too little data."""
    rets = []
    for a, b in zip(closes, closes[1:]):
        if a and b:
            rets.append(b / a - 1)
    if len(rets) < MIN_RETURNS:
        return None
    sd = statistics.pstdev(rets)
    return sd * math.sqrt(252) if sd > 0 else None


def _apply_cap(weights: dict[str, float], cap: float) -> dict[str, float]:
    w = dict(weights)
    for _ in range(len(w)):
        over = [k for k, v in w.items() if v > cap + 1e-12]
        if not over:
            break
        excess = sum(w[k] - cap for k in over)
        for k in over:
            w[k] = cap
        free = [k for k in w if k not in over and w[k] < cap - 1e-12]
        total = sum(w[k] for k in free)
        if not free or total == 0:
            break
        for k in free:
            w[k] += excess * w[k] / total
    return w


def suggest(items: list[dict[str, Any]], max_weight_pct: float = 35.0) -> tuple[dict[str, dict[str, Any]], float]:
    """items: [{"symbol", "vol" (fraction or None), "signal" ("LONG"/"NO_TRADE"/...)}]
    Returns (per-symbol info, unallocated fraction). Per-symbol info:
    {"weight" (fraction, 0 for non-LONG), "vol", "vol_estimated", "basis"}."""
    known = [i["vol"] for i in items if i.get("signal") == "LONG" and i.get("vol")]
    fallback = statistics.median(known) if known else None  # LONG stocks without enough history

    raw: dict[str, float] = {}
    meta: dict[str, dict[str, Any]] = {}
    for it in items:
        is_long = it.get("signal") == "LONG"
        own_vol = it.get("vol") or None
        vol = own_vol or (fallback if is_long else None)
        meta[it["symbol"]] = {"vol": vol, "vol_estimated": is_long and own_vol is None}
        if is_long:
            raw[it["symbol"]] = 1 / vol if vol else 1.0
    weights: dict[str, float] = {}
    if raw:
        total = sum(raw.values())
        weights = _apply_cap({k: v / total for k, v in raw.items()}, max_weight_pct / 100)
    out = {}
    for it in items:
        sym = it["symbol"]
        w = weights.get(sym, 0.0)
        out[sym] = {"weight": w, **meta[sym],
                    "basis": "LONG setup, weighted by calm" if sym in weights else "No trade: no allocation"}
    return out, max(0.0, 1.0 - sum(weights.values()))
