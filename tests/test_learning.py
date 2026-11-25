"""The learning layer: conditions, shrinkage, adoption only when it works on unseen setups, re-ranking, and honest A/B. Run: python tests/test_learning.py"""
import asyncio, datetime as dt, math, os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import learning  # noqa: E402
import recommend  # noqa: E402


def day(i): return (dt.date(2026, 1, 1) + dt.timedelta(i)).isoformat()


def rows_with_signal(n, seed=1, signal=True):
    """Resolved setups. With signal=True, setups found at RSI 60-70 lose and those at 40-50 win; otherwise returns are pure noise."""
    rnd, out = random.Random(seed), []
    for i in range(n):
        rsi = rnd.choice(["<40", "40-50", "50-60", "60-70", ">70"])
        base = {"<40": 0.2, "40-50": 1.4, "50-60": 0.4, "60-70": -1.3, ">70": -0.8}[rsi] if signal else 0.0
        ret = base + rnd.gauss(0, 3.0)
        out.append({"as_of": day(i // 3), "return_pct": ret, "features": {"setup": rnd.choice(["trend_momentum", "support_bounce"]), "score": rnd.choice(["3/4", "4/4"]),
                    "rr": rnd.choice(["<2", "2-2.5", ">2.5"]), "risk": rnd.choice(["<3%", "3-5%", ">5%"]), "rsi": rsi, "cap": "unknown", "liq": "top100", "regime": "unknown"}})
    return out


def test_conditions_are_bucketed():
    f = learning.features_for({"entry": 100, "stop_loss": 96, "target": 108, "score": "4/4", "rsi": 55.0, "setup": "support_bounce", "rank": 12}, 72, "mid")
    assert f == {"setup": "support_bounce", "score": "4/4", "rr": "2-2.5", "risk": "3-5%", "rsi": "50-60", "cap": "mid", "liq": "top30", "regime": "strong"}
    g = learning.features_for({"entry": 100, "stop_loss": 99, "target": 104, "score": "3/4"})
    assert g["rr"] == ">2.5" and g["risk"] == "<3%" and g["rsi"] == "unknown" and g["regime"] == "unknown" and g["liq"] == "unknown"


def test_small_groups_are_shrunk_toward_the_average():
    rows = [{"as_of": day(i), "return_pct": 0.0, "features": {"rsi": "<40"}} for i in range(200)]
    rows += [{"as_of": day(300 + i), "return_pct": 20.0, "features": {"rsi": "60-70"}} for i in range(3)]     # 3 lucky trades
    m = learning.calibrate(rows)
    b = m["buckets"]["rsi"]["60-70"]
    assert b["mean"] == 20.0 and b["shrunk"] < 4.0 < 20.0                          # three trades say little
    assert abs(learning.score(m, {"rsi": "60-70"})["edge"] - b["shrunk"]) < 1e-3 and learning.score(m, {"rsi": "unknown"})["n"] == 0


def test_adopts_a_real_pattern_and_rejects_noise():
    real = learning.train(rows_with_signal(600, seed=3, signal=True))
    v = real["validation"]
    assert real["adopted"] and len(v["folds"]) == 2 and all(f["pass"] for f in v["folds"]) and v["p_value"] <= learning.MAX_P
    assert v["lift"] >= learning.MIN_LIFT and v["n_valid"] >= 2 * learning.MIN_VALID and "both walk-forward tests" in real["reason"]
    false_adoptions = 0
    for seed in range(1, 41):                                                       # pure noise, many different draws
        noise = learning.train(rows_with_signal(600, seed=seed, signal=False))
        false_adoptions += noise["adopted"]
        assert noise["adopted"] or "plain rules keep ranking" in noise["reason"]
    assert false_adoptions <= 1, false_adoptions                                    # two independent tests keep luck out
    few = learning.train(rows_with_signal(150))
    assert not few["adopted"] and few["validation"] is None and "Needs at least" in few["reason"]


def test_learned_edge_orders_setups_sensibly():
    m = learning.train(rows_with_signal(800, seed=5))["model"]
    good = learning.score(m, {"rsi": "40-50", "rr": "2-2.5"})["edge"]
    bad = learning.score(m, {"rsi": "60-70", "rr": "2-2.5"})["edge"]
    assert good > bad + 1.0
    table = learning.bucket_table(m)
    assert table[0]["feature"] == "rsi" and table[0]["bucket"] == "40-50" and table[-1]["bucket"] == "60-70"


def _scan_result(model):
    longs = []
    for i, rsi in enumerate(["60-70", "40-50", "60-70", "40-50", "50-60", "60-70", "40-50"]):
        longs.append({"symbol": f"S{i}", "as_of": day(500), "score": "3/4", "entry": 100.0, "stop_loss": 96.0, "target": 108.0, "rsi": 50.0, "model": "trend",
                      "setup": "trend_momentum", "returns": {}, "features": {"setup": "trend_momentum", "score": "3/4", "rr": "2-2.5", "risk": "3-5%", "rsi": rsi,
                                                                              "cap": "unknown", "liq": "top100", "regime": "mixed"}})
    return {"model": "trend", "scanned": 7, "evaluated": 7, "failed": 0, "longs": longs, "regime": 55, "universe": {}}


def test_recommend_reranks_and_avoids_with_a_proven_model():
    trained = learning.train(rows_with_signal(800, seed=5)); trained["version"] = 3
    assert trained["adopted"]
    async def never(*a, **k): raise AssertionError("must not re-scan")
    plain = asyncio.run(recommend.recommend("u", [], 70000, never, never, scan_result=_scan_result("trend")))
    smart = asyncio.run(recommend.recommend("u", [], 70000, never, never, scan_result=_scan_result("trend"), learned=trained))
    assert plain["learning"] == {"used": False} and smart["learning"]["used"] and smart["learning"]["version"] == 3
    edges = [p["learned_edge"] for p in smart["picks"]]
    assert edges == sorted(edges, reverse=True) and all(p["features"]["rsi"] != "60-70" for p in smart["picks"][:3])   # the 60-70 RSI setups sink
    assert smart["learning"]["avoided"] >= 0 and "learned_n" in smart["picks"][0]
    assert [p["symbol"] for p in plain["picks"]] != [p["symbol"] for p in smart["picks"]]


def test_logging_ranks_and_the_live_comparison():
    scan = _scan_result("trend"); trained = learning.train(rows_with_signal(800, seed=5)); trained["version"] = 2
    base_only = learning.rows_from_scan(scan, None); with_model = learning.rows_from_scan(scan, trained)
    assert len(base_only) == 7 and all(r["rank_baseline"] == r["rank_learned"] and r["model_version"] is None for r in base_only)
    assert any(r["rank_baseline"] != r["rank_learned"] for r in with_model) and all(r["model_version"] == 2 for r in with_model)
    # live comparison: on days the learned model was in use, do its top picks beat the plain top picks?
    rows = []
    for d in range(12):
        for k in range(10):
            rows.append({"source": "scan", "as_of": day(d), "return_pct": (2.0 if k >= 5 else -1.0), "rank_baseline": k + 1, "rank_learned": 10 - k, "model_version": 2})
    ab = learning.ab_compare(rows)
    assert ab["days"] == 12 and ab["learned_avg"] > ab["plain_avg"] and ab["diff"] == 3.0
    assert learning.ab_compare([{**r, "model_version": None} for r in rows])["days"] == 0


def test_outcomes_and_backfill_from_prices():
    def series(seed, n=170):
        rnd, recs, d = random.Random(seed), [], dt.date(2025, 6, 2)
        for i in range(n):
            px = 100 + 12 * math.sin(i / 18) + i * 0.05 + rnd.gauss(0, 1.1); o = px + rnd.gauss(0, .5)
            while d.weekday() >= 5: d += dt.timedelta(1)
            recs.append({"date": d.isoformat(), "open": o, "high": max(o, px) + .7, "low": min(o, px) - .7, "close": px}); d += dt.timedelta(1)
        return recs
    recs = series(4)
    rows = learning.backfill_rows("AAA", recs, tier="large", rank=10)
    assert rows and all(r["source"] == "backfill" and r["status"] in ("target", "stopped", "expired") and r["features"]["cap"] == "large" and r["features"]["regime"] == "unknown" for r in rows)
    assert {r["model"] for r in rows} <= {"trend", "sr"} and all(r["setup"] in ("trend_momentum", "support_bounce", "resistance_breakout") for r in rows)
    # an open setup logged long ago is resolved against later prices
    entry = recs[60]["close"]; stop, target = entry * 0.97, entry * 1.06
    ups = learning.resolve([{"id": 7, "symbol": "AAA", "as_of": recs[60]["date"], "entry": entry, "stop_loss": stop, "target": target}], {"AAA": recs})
    assert ups and ups[0]["id"] == 7 and ups[0]["status"] in ("target", "stopped", "expired") and ups[0]["return_pct"] is not None
    assert learning.resolve([{"id": 8, "symbol": "ZZZ", "as_of": recs[60]["date"], "entry": 1, "stop_loss": .9, "target": 1.1}], {"AAA": recs}) == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
