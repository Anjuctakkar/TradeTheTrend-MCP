"""The suggestion scan: at least 250 stocks, topped up from the sector lists, one shared scan, then personalised. Run: python tests/test_recommend_scan.py"""
import asyncio, datetime as dt, math, os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import recommend  # noqa: E402


def series(seed, n=150):
    rnd = random.Random(seed)
    recs, d = [], dt.date(2026, 1, 1)
    for i in range(n):
        px = 100 + 12 * math.sin(i / 18) + i * 0.05 + rnd.gauss(0, 1.1); o = px + rnd.gauss(0, .5)
        while d.weekday() >= 5: d += dt.timedelta(1)
        recs.append({"date": d.isoformat(), "open": o, "high": max(o, px) + abs(rnd.gauss(0, .7)), "low": min(o, px) - abs(rnd.gauss(0, .7)), "close": px}); d += dt.timedelta(1)
    return recs


def test_universe_tops_up_to_250():
    async def call_tool(url, tool, args):                 # the tool only returns 120 rows
        assert tool == "get_top_by_volume" and args["n"] >= 250
        return {"content": [{"symbol": f"T{i}", "turnover": 1000 - i} for i in range(120)]}
    extra = [f"E{i}" for i in range(300)] + ["T3"]
    syms, info, uni = asyncio.run(recommend.universe("u", call_tool, extra))
    assert len(syms) == len(set(syms)) == 250 and uni["top_by_value"] == 120 and uni["from_sector_lists"] == 130
    assert info["T0"]["source"] == "top_by_value" and info["T0"]["traded_value"] == 1000 and info["E0"]["source"] == "sector_lists" and uni["note"]


def test_universe_survives_a_failed_list():
    async def call_tool(url, tool, args): raise RuntimeError("down")
    syms, info, uni = asyncio.run(recommend.universe("u", call_tool, [f"E{i}" for i in range(300)]))
    assert len(syms) == 250 and uni["top_by_value"] == 0 and "failed" in uni["note"]


def test_scan_is_wide_and_personalised():
    async def call_tool(url, tool, args): return {"content": [{"symbol": f"S{i}"} for i in range(260)]}
    seen = []
    async def fetch(url, sym, months):
        seen.append(sym)
        if sym == "S7": raise RuntimeError("no data")
        return series(int(sym[1:]))
    prog = []
    async def price(sym, entry, d): return {"cmp": entry, "source": "live", "as_of": "x", "change_pct": 1.5, "note": None}
    r = asyncio.run(recommend.recommend("u", [], 100000, fetch, call_tool, price_lookup=price, model="either", progress=lambda d, t: prog.append((d, t))))
    assert r["scanned"] >= 250 and len(seen) >= 250 and r["failed"] == 1 and r["evaluated"] == r["scanned"] - 1
    assert prog[-1] == (r["scanned"], r["scanned"]) and r["universe"]["total"] == r["scanned"]
    assert r["longs_found"] > 0 and 0 < len(r["picks"]) <= recommend.MAX_PICKS
    p = r["picks"][0]
    assert p["cmp_change_pct"] == 1.5 and p["quantity"] >= 0 and "returns" not in p and p["model"] in ("trend", "sr")


def test_shared_scan_is_reused_and_holdings_excluded():
    async def call_tool(url, tool, args): return {"content": [{"symbol": f"S{i}"} for i in range(250)]}
    n = {"calls": 0}
    async def fetch(url, sym, months): n["calls"] += 1; return series(int(sym[1:]))
    syms, info, uni = asyncio.run(recommend.universe("u", call_tool))
    sc = asyncio.run(recommend.scan("u", syms, info, fetch, "trend")); sc["universe"] = uni
    calls_after_scan = n["calls"]
    first = asyncio.run(recommend.recommend("u", [], 50000, fetch, call_tool, scan_result=sc))
    held = [{"symbol": first["picks"][0]["symbol"], "dates": [d["date"] for d in series(1)], "close": [d["close"] for d in series(1)]}]
    second = asyncio.run(recommend.recommend("u", held, 50000, fetch, call_tool, scan_result=sc))
    assert n["calls"] == calls_after_scan                                   # personalising never re-scans
    assert held[0]["symbol"] not in [p["symbol"] for p in second["picks"]]
    try:
        asyncio.run(recommend.recommend("u", [], 1, fetch, call_tool, model="bogus"))
    except ValueError:
        return
    raise AssertionError("bad model should be rejected")


def test_scan_size_choice_30_or_250():
    assert recommend.size_choice(30) == 30 and recommend.size_choice("30") == 30 and recommend.size_choice(10) == 30
    assert recommend.size_choice(250) == recommend.SCAN_SIZE >= 250 and recommend.size_choice(None) == recommend.SCAN_SIZE and recommend.size_choice("x") == recommend.SCAN_SIZE
    asked = []
    async def call_tool(url, tool, args):
        asked.append(args["n"])
        return {"content": [{"symbol": f"T{i}"} for i in range(max(args["n"], 400))]}      # a server that returns more than asked
    syms, info, uni = asyncio.run(recommend.universe("u", call_tool, [f"E{i}" for i in range(300)], 30))
    assert asked == [30] and len(syms) == 30 and uni["size"] == 30 and uni["from_sector_lists"] == 0
    syms, info, uni = asyncio.run(recommend.universe("u", call_tool, [f"E{i}" for i in range(300)], 250))
    assert asked[-1] >= 250 and len(syms) >= 250 and uni["size"] >= 250
    async def down(url, tool, args): raise RuntimeError("x")
    syms, _, uni = asyncio.run(recommend.universe("u", down, [f"E{i}" for i in range(300)], 30))
    assert len(syms) == 30 and uni["top_by_value"] == 0                                    # falls back to the sector lists, still 30


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
