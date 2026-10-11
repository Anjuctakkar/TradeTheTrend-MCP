"""
Funding with own capital + MTF (margin trading facility), interest, charges and net P/L.

Everything here is an ESTIMATE from rules each user controls (Settings > Funding rules, stored per user). It will not match a broker contract note to the rupee.

Funding split
  Each stock has an "own margin" m: the share of the position paid from your own money
  (m = 1 / leverage, so 2.7x -> 37.04%). The rest is funded by the broker. Given exposure
  weights w (they add up to at most 1; any remainder stays as own cash) the total exposure is

      T = own_capital / (sum(w*m) + 1 - sum(w))          (cash limit)
      T <= mtf_limit / sum(w*(1-m))                       (broker limit, if you set one)

  and each stock's position is w * T. With no MTF every m is 1 and T = own_capital.

Interest   funded * rate / day_count * calendar days held (interest runs on weekends and holidays).
Charges    itemised per leg: brokerage, STT, exchange charge, SEBI fee, stamp duty (buy), DP charge
           (sell), optional MTF pledge/unpledge fees, and GST on the GST-able ones.
Net P/L    (exit - entry) * qty - buy charges - sell charges - interest.
"""
import copy
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Optional

import holding
import user_store


# Brokerage, DP and pledge values are PLACEHOLDERS: set your broker's. The statutory charges
# follow current published NSE delivery rates and can change.
DEFAULT_RULES: dict[str, Any] = {
    "default_leverage": 2.7,      # -> own margin 1/2.7 = 37.04%
    "interest_pct": 9.6,          # % per annum on the funded amount
    "day_count": 365,
    "assumed_hold_days": 20,      # trading days, used only when no holding period is proposed
    "brokerage_pct": 0.1,         # % of trade value, per leg
    "brokerage_cap": 20.0,        # Rs per leg (0 = no cap)
    "brokerage_min": 0.0,
    "stt_pct": 0.1,               # delivery, both legs
    "exchange_pct": 0.00307,      # NSE cash, both legs
    "sebi_per_crore": 10.0,       # Rs per crore, both legs
    "stamp_buy_pct": 0.015,       # buy leg only
    "gst_pct": 18.0,              # on brokerage + exchange + SEBI + DP + pledge fees
    "dp_charge": 15.0,            # Rs per sell
    "pledge_fee": 0.0,            # Rs per MTF buy (pledge)
    "unpledge_fee": 0.0,          # Rs per MTF sell (unpledge)
}
LIMITS: dict[str, tuple[float, float]] = {
    "default_leverage": (1, 4), "interest_pct": (0, 40), "day_count": (360, 365), "assumed_hold_days": (1, 120),
    "brokerage_pct": (0, 5), "brokerage_cap": (0, 10000), "brokerage_min": (0, 10000), "stt_pct": (0, 1),
    "exchange_pct": (0, 0.5), "sebi_per_crore": (0, 1000), "stamp_buy_pct": (0, 1), "gst_pct": (0, 50),
    "dp_charge": (0, 1000), "pledge_fee": (0, 1000), "unpledge_fee": (0, 1000),
}


# ------------------------------------------------------------------ rules
def parse_stock_list(text: str) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """One stock per line: 'TCS 3x' or 'TCS 3' (leverage), 'TCS 25%' (own margin), 'TCS none' (not on MTF).
    '#' starts a comment. Returns ({SYMBOL: {"mtf": bool, "leverage": x}}, errors)."""
    stocks: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for n, raw in enumerate((text or "")[:20000].splitlines()[:200], 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p for p in re.split(r"[\s,:;=\t]+", line) if p]
        if len(parts) != 2:
            errors.append(f"Line {n}: expected 'SYMBOL value', got '{raw.strip()}'.")
            continue
        sym, val = parts[0].upper(), parts[1].lower()
        if not re.fullmatch(r"[A-Z0-9&_-]{1,20}", sym):
            errors.append(f"Line {n}: '{parts[0]}' doesn't look like an NSE symbol.")
            continue
        if val in ("none", "no", "off", "n", "non-mtf", "0"):
            stocks[sym] = {"mtf": False, "leverage": 1.0}
            continue
        try:
            if val.endswith("%"):
                margin = float(val[:-1])
                if not 25 <= margin <= 100:
                    raise ValueError("margin")
                lev = 100.0 / margin
            else:
                lev = float(val.rstrip("x"))
        except ValueError:
            errors.append(f"Line {n}: '{parts[1]}' isn't a leverage (like 3x), a margin (like 25%) or 'none'.")
            continue
        if not 1 <= lev <= 4:
            errors.append(f"Line {n}: leverage {lev:g}x for {sym} is outside 1x to 4x (the minimum own margin is 25%).")
            continue
        stocks[sym] = {"mtf": lev > 1, "leverage": round(lev, 4)}
    return stocks, errors


def stocks_to_text(stocks: dict[str, dict[str, Any]]) -> str:
    return "\n".join(f"{s} none" if not v.get("mtf") else f"{s} {v['leverage']:g}x" for s, v in sorted(stocks.items()))


def validate(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Check user-supplied rule values. Returns (clean rules, errors). Missing keys keep defaults."""
    rules = copy.deepcopy(DEFAULT_RULES)
    rules["stocks"] = {}
    errors: list[str] = []
    for key, (lo, hi) in LIMITS.items():
        if key not in data or data[key] in (None, ""):
            continue
        try:
            v = float(data[key])
        except (TypeError, ValueError):
            errors.append(f"{key}: '{data[key]}' isn't a number.")
            continue
        if not lo <= v <= hi:
            errors.append(f"{key}: {v:g} is outside {lo:g} to {hi:g}.")
            continue
        rules[key] = int(v) if key in ("day_count", "assumed_hold_days") else v
    if "stocks_text" in data:
        rules["stocks"], errs = parse_stock_list(data["stocks_text"] or "")
        errors += errs
    elif isinstance(data.get("stocks"), dict):
        rules["stocks"] = data["stocks"]
    return rules, errors


def default_rules() -> dict[str, Any]:
    rules = copy.deepcopy(DEFAULT_RULES)
    rules["stocks"], rules["customised"] = {}, False
    return rules


def load_rules(user_id: int) -> dict[str, Any]:
    """This user's saved rules, or the defaults if they have none (a new user always starts on defaults)."""
    saved = user_store.get_funding_rules(user_id)
    if not saved:
        return default_rules()
    clean, errors = validate(saved)
    if errors:
        return default_rules()
    clean["customised"] = True
    return clean


def save_rules(rules: dict[str, Any], user_id: int) -> None:
    user_store.set_funding_rules(user_id, {k: v for k, v in rules.items() if k != "customised"})


def reset_rules(user_id: int) -> None:
    user_store.clear_funding_rules(user_id)


def margin_for(symbol: str, rules: dict[str, Any]) -> dict[str, Any]:
    """Own-money share for a stock under MTF, from the per-stock list or the default leverage."""
    s = rules.get("stocks", {}).get(symbol)
    if s is not None:
        if not s.get("mtf"):
            return {"own": 1.0, "leverage": 1.0, "mtf": False, "source": "your list: not on MTF"}
        return {"own": 1 / s["leverage"], "leverage": s["leverage"], "mtf": True, "source": "your list"}
    lev = rules["default_leverage"]
    return {"own": 1 / lev, "leverage": lev, "mtf": lev > 1, "source": "default leverage"}


# ------------------------------------------------------------------ money maths
def leg_charges(side: str, value: float, rules: dict[str, Any], on_mtf: bool) -> dict[str, float]:
    """Charges for one leg ('buy' or 'sell') of a delivery trade of the given value."""
    r = rules
    brokerage = value * r["brokerage_pct"] / 100
    if r["brokerage_cap"] > 0:
        brokerage = min(brokerage, r["brokerage_cap"])
    brokerage = max(brokerage, r["brokerage_min"]) if value > 0 else 0.0
    c = {
        "brokerage": brokerage,
        "stt": value * r["stt_pct"] / 100,
        "exchange": value * r["exchange_pct"] / 100,
        "sebi": value * r["sebi_per_crore"] / 1e7,
        "stamp": value * r["stamp_buy_pct"] / 100 if side == "buy" else 0.0,
        "dp": r["dp_charge"] if side == "sell" and value > 0 else 0.0,
        "pledge": (r["pledge_fee"] if side == "buy" else r["unpledge_fee"]) if on_mtf and value > 0 else 0.0,
    }
    c["gst"] = r["gst_pct"] / 100 * (c["brokerage"] + c["exchange"] + c["sebi"] + c["dp"] + c["pledge"])
    c["total"] = sum(c.values())
    return c


def interest_for(funded: float, days: int, rules: dict[str, Any]) -> float:
    return funded * rules["interest_pct"] / 100 / rules["day_count"] * days


def net_pnl(entry: float, exit_price: float, qty: int, funded: float, days: int,
            rules: dict[str, Any], on_mtf: bool) -> dict[str, float]:
    gross = (exit_price - entry) * qty
    buy = leg_charges("buy", entry * qty, rules, on_mtf)["total"]
    sell = leg_charges("sell", exit_price * qty, rules, on_mtf)["total"]
    interest = interest_for(funded, days, rules)
    return {"gross": gross, "charges": buy + sell, "interest": interest, "net": gross - buy - sell - interest}


def break_even(entry: float, qty: int, funded: float, days: int, rules: dict[str, Any], on_mtf: bool) -> Optional[float]:
    """Exit price at which net P/L is zero (bisection, since brokerage is capped)."""
    lo, hi = entry, entry * 1.5
    if net_pnl(entry, hi, qty, funded, days, rules, on_mtf)["net"] < 0:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if net_pnl(entry, mid, qty, funded, days, rules, on_mtf)["net"] < 0:
            lo = mid
        else:
            hi = mid
    return hi


def plan_exposure(items: list[dict[str, Any]], own_capital: float, mtf_limit: Optional[float]) -> dict[str, Any]:
    """items: [{"weight", "own"}]. Returns total exposure T and which limit binds."""
    sw = sum(i["weight"] for i in items)
    sm = sum(i["weight"] * i["own"] for i in items)
    t_cash = own_capital / (sm + 1 - sw) if (sm + 1 - sw) > 0 else 0.0
    funded_share = sum(i["weight"] * (1 - i["own"]) for i in items)
    if mtf_limit and mtf_limit > 0 and funded_share > 0 and mtf_limit / funded_share < t_cash:
        return {"exposure": mtf_limit / funded_share, "binding": "limit"}
    return {"exposure": t_cash, "binding": "cash"}


def calendar_days(last_date: str, trading_days: int) -> tuple[int, str]:
    review = holding._add_business_days(last_date[:10], trading_days)
    return (dt.date.fromisoformat(review) - dt.date.fromisoformat(last_date[:10])).days, review


def _r2(d: dict[str, float]) -> dict[str, float]:
    return {k: round(v, 2) for k, v in d.items()}


def stock_funding(r: dict[str, Any], rules: dict[str, Any], use_mtf: bool) -> dict[str, Any]:
    """Own money, funded amount, interest, and net P/L at target and stop for one analysed stock."""
    qty = r.get("quantity") or 0
    price = r.get("latest_price") or 0
    m = margin_for(r["symbol"], rules) if use_mtf else {"own": 1.0, "leverage": 1.0, "mtf": False, "source": "own money only"}
    position = qty * price
    funded = round(position * (1 - m["own"]), 2) if position else 0.0
    out: dict[str, Any] = {
        "own_margin_pct": round(m["own"] * 100, 2), "leverage": round(m["leverage"], 3), "mtf": m["mtf"] and funded > 0,
        "margin_source": m["source"], "position": round(position, 2), "funded": funded, "own": round(position - funded, 2),
        "interest_per_day": round(interest_for(funded, 1, rules), 2), "outcomes": None,
    }
    sig = r.get("trade_signal") or {}
    if sig.get("signal") != "LONG" or not qty:
        return out
    h = r.get("hold") or {}
    proposed = h.get("status") == "proposed"
    n = h["days"] if proposed else rules["assumed_hold_days"]
    cal, review = calendar_days(r["ltp_date"], n)
    entry, target, stop = sig["entry"], sig["target"], sig["stop_loss"]
    on_mtf = out["mtf"]
    t_net = net_pnl(entry, target, qty, funded, cal, rules, on_mtf)
    s_net = net_pnl(entry, stop, qty, funded, cal, rules, on_mtf)
    be = break_even(entry, qty, funded, cal, rules, on_mtf)
    own = out["own"]
    out.update({
        "hold_days": n, "hold_basis": "proposed" if proposed else "assumed", "calendar_days": cal, "review_by": review,
        "interest_to_review": round(interest_for(funded, cal, rules), 2),
        "outcomes": {
            "entry": entry, "target": target, "stop": stop,
            "target_net": _r2(t_net), "stop_net": _r2(s_net),
            "break_even": None if be is None else round(be, 2),
            "break_even_pct": None if be is None else round((be / entry - 1) * 100, 2),
            "gross_rr": sig.get("reward_risk_ratio"),
            "net_rr": round(t_net["net"] / -s_net["net"], 2) if s_net["net"] < 0 and t_net["net"] > 0 else None,
            "target_pct_own": round(t_net["net"] / own * 100, 1) if own else None,
            "stop_pct_own": round(s_net["net"] / own * 100, 1) if own else None,
        },
    })
    return out


def summarize(ok: list[dict[str, Any]], own_capital: float, plan: Optional[dict[str, Any]],
              mtf_limit: Optional[float], use_mtf: bool, rules: dict[str, Any]) -> dict[str, Any]:
    f = [r["funding"] for r in ok if r.get("funding")]
    own_used, funded = sum(x["own"] for x in f), sum(x["funded"] for x in f)
    exposure = sum(x["position"] for x in f)
    return {
        "mtf": use_mtf, "own_capital": round(own_capital, 2), "own_used": round(own_used, 2), "funded": round(funded, 2),
        "exposure": round(exposure, 2), "leverage": round(exposure / own_used, 2) if own_used else None,
        "interest_per_day": round(sum(x["interest_per_day"] for x in f), 2),
        "unused_own": round(own_capital - own_used, 2),
        "mtf_limit": mtf_limit if mtf_limit and mtf_limit > 0 else None,
        "limit_binding": bool(plan and plan["binding"] == "limit"),
        "interest_pct": rules["interest_pct"], "default_leverage": rules["default_leverage"],
        "rules_customised": rules.get("customised", False),
    }
