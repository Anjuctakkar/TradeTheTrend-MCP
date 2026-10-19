"""Checks for sectors.py. Run: python tests/test_sectors.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import sectors  # noqa: E402


def test_index_and_resample():
    days = [f"2026-09-{d:02d}" for d in range(1, 31) if d % 7 not in (6, 0)]
    a = {d: 100 * 1.01 ** i for i, d in enumerate(days)}           # +1% a day
    b = {d: 50 for d in days}                                       # flat
    dates, v = sectors.equal_weight_index({"A": a, "B": b})
    assert v[0] == 100 and abs(v[1] - 100.5) < 1e-6                 # average of +1% and 0%
    w = sectors.resample(dates, v, "weekly"); m = sectors.resample(dates, v, "monthly")
    assert len(m["dates"]) == 1 and m["values"][0] == v[-1] and len(w["dates"]) < len(dates)


def test_rank_and_status():
    def mk(m1, w1, m3, above, br):
        g = {"metrics": {"d1": 0, "w1": w1, "m1": m1, "m3": m3, "m6": None, "above_sma20": above, "breadth50": br, "as_of": "x"}, "used": 5, "total": 5}
        return {"segments": {"all": g}, "symbols": list("abcde")}
    r = sectors.rank({"Up": mk(8, 2, 15, True, 80), "Down": mk(-6, -2, -9, False, 10), "Mid": mk(1, -1, 3, True, 40)})
    assert [x["name"] for x in r] == ["Up", "Mid", "Down"]
    assert r[0]["status"] == "gaining" and r[-1]["status"] == "losing" and r[1]["status"] == "mixed"
    assert sectors.rank({"Up": mk(8, 2, 15, True, 80)}, "large") == []        # a cap view that isn't there is skipped


def test_default_classification():
    S = sectors.DEFAULT_SECTORS
    assert list(S) == ["Commodities", "Consumer Discretionary", "Energy", "FMCG", "Financial Services", "Healthcare",
                       "Industrials", "Information Technology", "Services", "Telecommunication", "Utilities", "Diversified"]
    assert sum(len(v) for v in S.values()) == 38                       # industry groups, as in the classification
    total = set()
    for name, inds in S.items():
        seen = []
        for ind, tiers in inds.items():
            assert tiers and all(sectors._ok_symbol(s) for s in sectors.symbols_of(tiers)), (name, ind)
            seen += sectors.symbols_of(tiers)
        assert len(seen) == len(set(seen)), name                         # no stock twice within a sector
        total |= set(seen)
    assert len(total) > 180


def test_assemble_segments_and_industries():
    import datetime as dt
    recs = lambda g: [{"date": (dt.date(2026, 1, 1) + dt.timedelta(i)).isoformat(), "open": 100 * g ** i, "high": 101 * g ** i, "low": 99 * g ** i, "close": 100 * g ** i} for i in range(80)]
    sector = {"Ind A": {"large": ["L1"], "mid": ["M1"]}, "Ind B": {"small": ["S1"]}}
    d = sectors.assemble("T", sector, {"L1": recs(1.01), "M1": recs(1.0), "S1": recs(0.99)}, {})
    assert set(d["segments"]) == {"all", "large", "midsmall"} and set(d["industries"]) == {"Ind A", "Ind B"}
    assert d["segments"]["large"]["metrics"]["m1"] > d["segments"]["midsmall"]["metrics"]["m1"]
    assert {s["cap"] for s in d["stocks"]} == {"large", "mid", "small"} and {s["industry"] for s in d["stocks"]} == {"Ind A", "Ind B"}
    rows = sectors.rank_industries({"T": d})
    assert {(r["sector"], r["industry"]) for r in rows} == {("T", "Ind A"), ("T", "Ind B")}
    assert sectors.rank_industries({"T": d}, "large")[0]["industry"] == "Ind A"          # Ind B has no large caps


def test_live_price_drives_1d():
    import datetime as dt
    recs = [{"date": (dt.date(2026, 1, 1) + dt.timedelta(i)).isoformat(), "open": 100, "high": 101, "low": 99, "close": 100.0} for i in range(80)]
    last = recs[-1]["date"]                                                # 2026-03-21
    nxt = (dt.date.fromisoformat(last) + dt.timedelta(2)).isoformat()
    sector = {"I": {"large": ["A", "B"]}}
    quote = lambda cmp, day, **kw: {"cmp": cmp, "source": "live", "as_of_iso": day + "T10:00:00+05:30", "prev_close": 100.0, **kw}
    d = sectors.assemble("S", sector, {"A": recs, "B": recs}, {}, {"A": quote(102.0, nxt), "B": quote(100.0, nxt)})
    a = next(s for s in d["stocks"] if s["symbol"] == "A")
    assert a["d1"] == 2.0 and a["ltp"] == 102.0 and a["ltp_source"] == "live" and a["close"] == 102.0
    assert abs(d["segments"]["all"]["metrics"]["d1"] - 1.0) < 1e-6 and d["as_of"] == nxt          # average of +2% and 0%
    assert d["history_to"] == last and d["live"]["with_ltp"] == 2 and d["live"]["live"] == 2
    # stale quote, same-day quote, and a quote whose previous close disagrees are not added as a new bar
    assert sectors.live_bar(quote(102.0, "2026-03-01"), last, 100.0) is None
    same = sectors.live_bar(quote(100.5, last, source="last_trade"), last, 100.0)
    assert same and same["bar"] is False
    assert sectors.live_bar(quote(102.0, nxt, prev_close=60.0), last, 100.0) is None
    assert sectors.live_bar({"cmp": 99.0, "source": "eod", "as_of_iso": nxt + "T10:00:00+05:30"}, last, 100.0) is None
    # no live info at all: 1D comes from the end-of-day closes, as before
    e = sectors.assemble("S", sector, {"A": recs}, {}, None)
    assert e["stocks"][0]["ltp"] is None and e["live"]["with_ltp"] == 0


def _loaded(paths):
    """Fake loaded sectors: {name: list of daily index values} -> the structure rotation() reads."""
    import datetime as dt
    days, d = [], dt.date(2026, 1, 1)
    while len(days) < len(next(iter(paths.values()))):
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += dt.timedelta(1)
    out = {}
    for n, vals in paths.items():
        ser = {tf: sectors.resample(days, vals, tf) for tf in sectors.TFS}
        out[n] = {"segments": {"all": {"series": ser}}, "industries": {}}
    return out


def test_quadrants():
    assert [sectors.quadrant(*x) for x in ((101, 101), (101, 99), (99, 99), (99, 101))] == ["leading", "weakening", "lagging", "improving"]


def test_rotation_finds_risers_fallers_and_early_turns():
    def path(n, rate_by_day):
        v, out = 100.0, []
        for i in range(n):
            v *= 1 + rate_by_day(i)
            out.append(v)
        return out
    flat = lambda n: [100.0] * n
    # a sector that slides, then turns up, is "improving" (an early mover) before it reaches "leading"
    n = 88
    turn = path(n, lambda i: -0.004 if 30 < i <= 80 else 0.006 if i > 80 else 0.0)
    r = sectors.rotation(_loaded({"F1": flat(n), "F2": flat(n), "F3": flat(n), "Turn": turn}), "weekly")
    t = next(x for x in r["items"] if x["name"] == "Turn")
    assert t["quadrant"] == "improving" and t["early"] and t["mom"] > 100 and t["ratio"] < 100
    assert r["items"][0]["name"] == "Turn"                                   # early movers are listed first
    # a sector that takes off from flat is leading with positive momentum; one that slides is lagging
    n = 108
    r = sectors.rotation(_loaded({"F1": flat(n), "F2": flat(n), "Rise": path(n, lambda i: 0.004 if i > 90 else 0.0),
                                  "Fall": path(n, lambda i: -0.004 if i > 90 else 0.0)}), "weekly")
    by = {x["name"]: x for x in r["items"]}
    assert by["Rise"]["quadrant"] == "leading" and by["Rise"]["mom"] > 100
    assert by["Fall"]["quadrant"] == "lagging" and by["Fall"]["mom"] < 100
    assert sum(r["counts"].values()) == 4 and len(by["Rise"]["trail"]) >= 2
    d = sectors.rotation(_loaded({"F1": flat(n), "F2": flat(n), "Rise": path(n, lambda i: 0.004 if i > 90 else 0.0)}), "daily")
    assert d["items"] and all(it["quadrant"] in ("leading", "improving", "weakening", "lagging") for it in d["items"])


def test_rotation_needs_enough_sectors_and_history():
    assert "at least 3" in sectors.rotation(_loaded({"A": [100.0] * 100, "B": [100.0] * 100}))["warning"]
    short = sectors.rotation(_loaded({"A": [100.0] * 30, "B": [100.0] * 30, "C": [100.0] * 30}), "weekly")
    assert short["warning"] and not short["items"]
    assert "Load this sector" in sectors.rotation({}, level="industries", sector="X")["warning"]


def test_sector_map_merges_with_builtins():
    import json, pathlib, tempfile
    tmp = pathlib.Path(tempfile.mkdtemp())
    old = sectors.config.DATA_DIR
    sectors.config.DATA_DIR = tmp
    try:
        assert sectors.load_sectors() is sectors.DEFAULT_SECTORS and sectors.map_status()["exists"] is False
        (tmp / "sector_map.json").write_text(json.dumps({"energy": {"oil, gas & consumable fuels": {"small": ["chennpetro"]}},
                                                         "Industrials": {"Capital Goods": {"small": ["ELECON"]}}, "Quick": ["TCS"]}))
        m = sectors.load_sectors()
        oil = m["Energy"]["Oil, Gas & Consumable Fuels"]
        assert "CHENNPETRO" in oil["small"] and "RELIANCE" in oil["large"]                     # added, built-ins kept
        assert "ELECON" in m["Industrials"]["Capital Goods"]["small"] and "TITAN" in sectors.all_symbols(m["Consumer Discretionary"])
        assert m["Quick"]["Quick"]["large"] == ["TCS"] and len(m) == len(sectors.DEFAULT_SECTORS) + 1
        assert "CHENNPETRO" not in sectors.DEFAULT_SECTORS["Energy"]["Oil, Gas & Consumable Fuels"].get("small", [])   # defaults untouched
        st = sectors.map_status()
        assert st["exists"] and st["mode"] == "merge" and st["entries"] == 3 and st["path"].endswith("sector_map.json")
        (tmp / "sector_map.json").write_text(json.dumps({"_mode": "replace", "Only": ["TCS", "INFY"]}))
        assert list(sectors.load_sectors()) == ["Only"] and sectors.map_status()["mode"] == "replace"
        (tmp / "sector_map.json").write_text("{ not json")
        assert sectors.load_sectors() is sectors.DEFAULT_SECTORS and sectors.map_status()["problem"]
    finally:
        sectors.config.DATA_DIR = old


def test_picks_shortlist():
    def stock(sym, m1, above=True, trend=False, sr=False, cap="large", w1=1.0, ltp="live"):
        return {"symbol": sym, "close": 100.0, "d1": 0.5, "w1": w1, "m1": m1, "above_sma50": above, "trend_long": trend, "sr_long": sr,
                "sr_setup": "support_bounce" if sr else None, "cap": cap, "industry": "Ind", "ltp_source": ltp}
    def sec(m1, w1, m3, above, br, stocks):
        g = {"metrics": {"d1": 0, "w1": w1, "m1": m1, "m3": m3, "m6": None, "above_sma20": above, "breadth50": br, "as_of": "x"}, "used": 5, "total": 5, "series": {}}
        return {"segments": {"all": g}, "stocks": stocks, "symbols": [s["symbol"] for s in stocks], "industries": {}}
    up = [stock("GOOD", 8.0, trend=True, sr=True), stock("BELOW", 8.0, above=False, trend=True), stock("LAG", 1.0), stock("NEG", -2.0, trend=True),
          stock("SMALLUP", 30.0, trend=True, cap="small", ltp=None)] + [stock(f"X{i}", 6.0, trend=True) for i in range(6)]
    loaded = {"Up": sec(5.0, 2.0, 12.0, True, 80, up), "Down": sec(-6.0, -2.0, -9.0, False, 10, [stock("WEAKSECTOR", 3.0, trend=True)])}
    r = sectors.picks(loaded)
    syms = [p["symbol"] for p in r["picks"]]
    assert syms[0] == "GOOD" and r["picks"][0]["score"] > r["picks"][1]["score"]
    assert "BELOW" not in syms and "LAG" not in syms and "NEG" not in syms and "WEAKSECTOR" not in syms     # rules applied
    assert len(syms) == 4 and all(p["sector"] == "Up" for p in r["picks"])                                   # per-sector cap
    assert r["rotation_warning"] and any("Trend" in x or "LONG" in x for x in r["picks"][0]["reasons"])
    small = sectors.picks(loaded, per_sector=10)["picks"]
    sm = next(p for p in small if p["symbol"] == "SMALLUP")
    assert any("extended" in c for c in sm["cautions"]) and any("small cap" in c for c in sm["cautions"]) and any("live" in c for c in sm["cautions"])
    assert not any(p["cap"] == "large" for p in sectors.picks(loaded, cap="midsmall")["picks"]) if "midsmall" in loaded["Up"]["segments"] else True
    assert sectors.picks({}, limit=5)["picks"] == []


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)
