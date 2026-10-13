"""Checks for the support/resistance rule set and the pieces that use it. Run: python -m pytest tests/test_sr_model.py
(or just: python tests/test_sr_model.py). Needs only the standard library plus the project's own modules."""
import asyncio
import datetime as dt
import math
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import backtest  # noqa: E402
import holding  # noqa: E402
import recommend  # noqa: E402
import sr_model  # noqa: E402
import technical  # noqa: E402


def series(seed: int, n: int = 400) -> list[dict]:
    random.seed(seed)
    recs, d = [], dt.date(2025, 1, 1)
    for i in range(n):
        px = 100 + 12 * math.sin(i / 18) + i * 0.04 + random.gauss(0, 1.2)
        o = px + random.gauss(0, 0.6)
        while d.weekday() >= 5:
            d += dt.timedelta(1)
        recs.append({"date": d.isoformat(), "open": o, "high": max(o, px) + abs(random.gauss(0, 0.8)),
                     "low": min(o, px) - abs(random.gauss(0, 0.8)), "close": px})
        d += dt.timedelta(1)
    return recs


def test_pivots_need_confirmation():
    p = backtest.prepare(series(1))
    n = len(p["c"])
    assert all(idx < n - sr_model.PIVOT_K for idx, _, _ in sr_model.find_pivots(p["h"], p["l"]))


def test_signal_ignores_future_bars():
    recs = series(2)
    full, part = backtest.prepare(recs), backtest.prepare(recs[:250])
    for i in (120, 180, 249):
        assert sr_model.signal_from_prepared(full, i) == sr_model.signal_from_prepared(part, i)


def test_long_signals_are_well_formed():
    seen = 0
    for seed in range(1, 6):
        p = backtest.prepare(series(seed))
        for i in range(70, len(p["c"]), 5):
            s = sr_model.signal_from_prepared(p, i)
            assert s["model"] == "sr"
            if s["signal"] == "LONG":
                seen += 1
                assert 0 < s["stop_loss"] < s["entry"] < s["target"]
                assert s["reward_risk_ratio"] >= sr_model.MIN_RR - 0.01
                assert s["setup"] in ("support_bounce", "resistance_breakout")
                assert int(s["score"][0]) >= 3
    assert seen > 0


def test_short_history_is_no_trade():
    p = backtest.prepare(series(1, 40))
    assert sr_model.generate_sr_signal(p["o"], p["h"], p["l"], p["c"])["signal"] == "NO_TRADE"


def test_default_backtest_unchanged_and_sr_runs():
    recs = series(3)
    assert backtest.run(recs) == backtest.run(recs, signal_fn=None)
    m = backtest.run(recs, signal_fn=sr_model.signal_from_prepared)["metrics"]
    assert m["trades"] >= 0 and m["tested_bars"] > 0


def test_trend_signal_is_tagged_and_compared():
    c = [100 + i * 0.1 for i in range(80)]
    sig = technical.generate_trade_signal(c, technical.sma(c, 20), technical.sma(c, 50), technical.rsi(c, 14), *technical.macd(c))
    assert sig["model"] == "trend"
    assert technical.compare_signals({"signal": "LONG"}, {"signal": "LONG"})["status"] == "both_long"
    assert technical.compare_signals({"signal": "NO_TRADE"}, {"signal": "NO_TRADE"})["status"] == "neither"


def test_holding_uses_matching_rule_set():
    recs = series(3)
    for end in range(150, 400, 10):
        sub = recs[:end]
        p = backtest.prepare(sub)
        s = sr_model.generate_sr_signal(p["o"], p["h"], p["l"], p["c"])
        if s["signal"] == "LONG":
            assert holding.propose(s, sub, None, model="sr")["status"] in ("proposed", "none")
            return
    raise AssertionError("no LONG window found to test")


def test_recommend_models():
    async def call_tool(url, tool, args):
        return {"content": [{"symbol": f"S{i}"} for i in range(8)]}

    async def fetch(url, sym, months):
        return series(int(sym[1:]) + 1, 150)

    for m in recommend.MODELS:
        r = asyncio.run(recommend.recommend("u", [], 100000, fetch, call_tool, model=m))
        assert r["model"] == m and all(p["model"] in ("trend", "sr") for p in r["picks"])
    try:
        asyncio.run(recommend.recommend("u", [], 1, fetch, call_tool, model="bogus"))
    except ValueError:
        return
    raise AssertionError("bad model should be rejected")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
