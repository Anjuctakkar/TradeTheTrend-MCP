"""
Small, dependency-free technical indicator helpers.

Everything takes/returns a list the same length as the input `values`
list, with `None` wherever there isn't enough history yet to compute a
value. These are the standard textbook formulas (simple moving average,
Wilder-style smoothed RSI is NOT used here - this is the simpler SMA-based
RSI - and a standard EMA-based MACD). They're meant for a quick visual
read, not for production trading decisions.
"""

from typing import Optional


def sma(values: list[Optional[float]], window: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    for i in range(len(values)):
        if i + 1 < window:
            continue
        chunk = values[i + 1 - window : i + 1]
        if any(v is None for v in chunk):
            continue
        out[i] = sum(chunk) / window
    return out


def ema(values: list[Optional[float]], window: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    k = 2 / (window + 1)
    prev: Optional[float] = None
    for i, v in enumerate(values):
        if v is None:
            out[i] = None
            continue
        prev = v if prev is None else (v * k + prev * (1 - k))
        out[i] = prev
    return out


def rsi(values: list[Optional[float]], window: int = 14) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    gains: list[Optional[float]] = [None] * len(values)
    losses: list[Optional[float]] = [None] * len(values)

    for i in range(1, len(values)):
        a, b = values[i - 1], values[i]
        if a is None or b is None:
            continue
        change = b - a
        gains[i] = max(change, 0.0)
        losses[i] = max(-change, 0.0)

    for i in range(len(values)):
        if i + 1 < window + 1:
            continue
        g_chunk = gains[i + 1 - window : i + 1]
        l_chunk = losses[i + 1 - window : i + 1]
        if any(g is None for g in g_chunk) or any(l is None for l in l_chunk):
            continue
        avg_gain = sum(g_chunk) / window
        avg_loss = sum(l_chunk) / window
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100 - (100 / (1 + rs))
    return out


def macd(
    values: list[Optional[float]], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[Optional[float]], list[Optional[float]]]:
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    macd_line: list[Optional[float]] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(ema_fast, ema_slow)
    ]
    signal_line = ema(macd_line, signal)
    return macd_line, signal_line


def latest_value(values: list[Optional[float]]) -> Optional[float]:
    for v in reversed(values):
        if v is not None:
            return v
    return None


def generate_trade_signal(
    closes: list[Optional[float]],
    sma20: list[Optional[float]],
    sma50: list[Optional[float]],
    rsi_series: list[Optional[float]],
    macd_line: list[Optional[float]],
    macd_signal: list[Optional[float]],
) -> dict:
    """A simple, fully mechanical long/no-trade setup from trend + momentum +
    RSI zone. This is a rule-based heuristic for a quick read, NOT investment
    advice or a recommendation — it only ever proposes a LONG setup (no
    shorts) and defaults to NO_TRADE whenever the checks don't line up.

    Rules (each worth 1 point out of 4):
      1. Trend    : 20-day SMA > 50-day SMA
      2. Strength : last close > 20-day SMA
      3. Momentum : MACD line > MACD signal line
      4. RSI zone : 40 <= RSI <= 65 (avoids chasing overbought, avoids
                     catching a falling knife in oversold territory)

    Hard vetoes to NO_TRADE regardless of score:
      - No confirmed uptrend (rule 1 fails)
      - RSI > 70 (overbought — poor risk/reward for a fresh long)
      - Fewer than 3/4 rules pass

    When a LONG setup is produced:
      - Entry   = last close
      - Stop    = the lower of (10-day swing low) and (2% below 20-day SMA)
                  falling back to 3% below entry if that's not below entry
      - Target  = entry + 2x the entry-to-stop risk (2:1 reward:risk)
    """
    last_close = latest_value(closes)
    last_sma20 = latest_value(sma20)
    last_sma50 = latest_value(sma50)
    last_rsi = latest_value(rsi_series)
    last_macd = latest_value(macd_line)
    last_macd_signal = latest_value(macd_signal)

    if None in (last_close, last_sma20, last_sma50, last_rsi, last_macd, last_macd_signal):
        return {
            "signal": "NO_TRADE",
            "score": None,
            "reasons": ["Not enough price history yet to compute all indicators (need 50+ trading days)."],
        }

    reasons: list[str] = []
    points = 0

    trend_up = last_sma20 > last_sma50
    reasons.append(
        f"Trend: 20-SMA {'above' if trend_up else 'not above'} 50-SMA ({last_sma20:.2f} vs {last_sma50:.2f})"
    )
    points += int(trend_up)

    strength = last_close > last_sma20
    reasons.append(f"Price {'above' if strength else 'below'} 20-SMA ({last_close:.2f} vs {last_sma20:.2f})")
    points += int(strength)

    momentum = last_macd > last_macd_signal
    reasons.append(
        f"MACD {'above' if momentum else 'at/below'} signal line ({last_macd:.2f} vs {last_macd_signal:.2f})"
    )
    points += int(momentum)

    rsi_ok = 40 <= last_rsi <= 65
    rsi_overbought = last_rsi > 70
    if rsi_ok:
        reasons.append(f"RSI {last_rsi:.1f} — healthy range")
    elif rsi_overbought:
        reasons.append(f"RSI {last_rsi:.1f} — overbought")
    elif last_rsi < 30:
        reasons.append(f"RSI {last_rsi:.1f} — oversold, no confirmed reversal")
    else:
        reasons.append(f"RSI {last_rsi:.1f} — neutral")
    points += int(rsi_ok)

    veto = (not trend_up) or rsi_overbought or points < 3
    if veto:
        return {"signal": "NO_TRADE", "score": f"{points}/4", "reasons": reasons}

    lookback = [c for c in closes[-10:] if c is not None]
    swing_low = min(lookback) if lookback else last_close * 0.97
    stop = min(swing_low, last_sma20 * 0.98)
    if stop >= last_close:
        stop = last_close * 0.97

    risk = last_close - stop
    target = last_close + 2 * risk

    return {
        "signal": "LONG",
        "score": f"{points}/4",
        "reasons": reasons,
        "entry": round(last_close, 2),
        "stop_loss": round(stop, 2),
        "target": round(target, 2),
        "risk_per_share": round(risk, 2),
        "reward_risk_ratio": round((target - last_close) / risk, 2) if risk else None,
    }
