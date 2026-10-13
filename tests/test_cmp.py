"""The current market price (CMP) is used in every workflow, with safe fallbacks. Run: PYTHONPATH=. python tests/test_cmp.py"""
import os, tempfile, asyncio, math, json, datetime as dt
os.environ.update(DASHBOARD_DATA_DIR=tempfile.mkdtemp()); os.environ.pop("GOOGLE_CLIENT_ID", None)
from fastapi.testclient import TestClient
import nse_cache, recommend, server, agent_tools, technical
nse_cache.ENABLED = False                      # every call must reach the fake feed
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
OK = lambda content: {"is_error": False, "content": content}

# a price series that produces a LONG setup: entry 115.52, stop 112.92, target 120.72
CLOSES = [round(100 + 0.2 * i + 3 * math.sin(i / 5), 2) for i in range(80)]
DAYS = [(dt.date.today() - dt.timedelta(days=80 - i)).isoformat() for i in range(80)]          # last bar is yesterday
RECORDS = [{"date": d, "open": c, "high": c, "low": c, "close": c} for d, c in zip(DAYS, CLOSES)]
ENTRY, STOP, TARGET = 115.52, 112.92, 120.72
assert CLOSES[-1] == ENTRY

state = {"market": "open", "ltp": 116.5, "age": 30, "fail": False, "error": False, "no_price": False, "quote_date_offset": 0, "calls": []}
def stamp(age, day_offset=0):
    return (dt.datetime.now(IST) - dt.timedelta(seconds=age, days=day_offset)).strftime("%Y-%m-%d %H:%M:%S")
async def fake_raw(url, tool, args):
    state["calls"].append(tool)
    if tool == "cm_get_stock_quote":
        if state["fail"]: raise RuntimeError("NSE did not reply")
        if state["error"]: return {"is_error": True, "content": "bad symbol"}
        stock = {"symbol": args["symbol"], "preClosePrice": ENTRY, "perChange": 0.85, "latestTimestamp": stamp(state["age"], state["quote_date_offset"]),
                 "fiftyTwoWeekHigh": 150, "fiftyTwoWeekLow": 90}
        if not state["no_price"]: stock["lastTradedPrice"] = state["ltp"]
        return OK({"updatedAt": "2026-10-12T04:30:27Z", "stock": stock})
    if tool == "get_stock_history": return OK({"data": RECORDS, "next_end_date": None})
    if tool == "get_corporate_actions": return OK([])
    if tool == "get_ltp_by_date": return OK(ENTRY)
    if tool == "moving_average": return OK(114.0)
    return OK({})
server._call_tool_raw = fake_raw
server._market_status = lambda: {"state": state["market"], "why": "", "now_ist": "", "note": ""}
def reset(**kw): state.update(market="open", ltp=116.5, age=30, fail=False, error=False, no_price=False, quote_date_offset=0, calls=[]); state.update(kw)

async def unit():
    sig = technical.generate_trade_signal(CLOSES, technical.sma(CLOSES, 20), technical.sma(CLOSES, 50), technical.rsi(CLOSES, 14), *technical.macd(CLOSES))
    assert sig["signal"] == "LONG" and (sig["entry"], sig["stop_loss"], sig["target"]) == (ENTRY, STOP, TARGET), sig

    # ---- the shared price function and its fallbacks
    reset(); i = await server.get_cmp("TCS", ENTRY, DAYS[-1])
    assert (i["cmp"], i["source"], i["change_pct"], i["prev_close"]) == (116.5, "live", 0.85, ENTRY) and i["as_of"], i
    reset(market="closed"); assert (await server.get_cmp("TCS", ENTRY, DAYS[-1]))["source"] == "last_trade"       # market closed: last trade
    reset(age=3600); assert (await server.get_cmp("TCS", ENTRY, DAYS[-1]))["source"] == "last_trade"               # open, but the quote is an hour old
    reset(quote_date_offset=5); i = await server.get_cmp("TCS", ENTRY, DAYS[-1])                                    # quote older than the EOD data
    assert (i["cmp"], i["source"]) == (ENTRY, "eod") and "older" in i["note"], i
    for kw in ({"fail": True}, {"error": True}, {"no_price": True}):                                                # feed problems never raise
        reset(**kw); i = await server.get_cmp("TCS", ENTRY, DAYS[-1]); assert (i["cmp"], i["source"]) == (ENTRY, "eod") and i["note"], (kw, i)
    reset(fail=True); i = await server.get_cmp("TCS"); assert (i["cmp"], i["source"]) == (None, "none")

    # ---- one-stock analysis: everything at the CMP, signal still from the close
    reset(); r = await server._analyze_one(server._server_url("bhavcopy"), server.StockAllocation(symbol="TCS", allocation=100000), dt.date.today().isoformat())
    assert not r.get("error"), r
    assert (r["latest_price"], r["cmp"], r["cmp_source"], r["eod_close"], r["ltp_date"]) == (116.5, 116.5, "live", ENTRY, DAYS[-1]), r
    assert r["trade_signal"]["entry"] == ENTRY                                                                       # the signal does not move with the price
    assert r["quantity"] == int(100000 // 116.5) and r["invested"] == round(r["quantity"] * 116.5, 2)               # sized at the CMP
    assert r["cmp_check"]["status"] == "ok" and r["buy_price"] == 116.5 and r["cmp_check"]["reward_risk_now"] == round((TARGET - 116.5) / (116.5 - STOP), 2)
    assert r["week52"]["price"] == 116.5 and r["week52"]["price_source"] == "live"
    reset(ltp=121.5); r = await server._analyze_one(server._server_url("bhavcopy"), server.StockAllocation(symbol="TCS", allocation=100000), dt.date.today().isoformat())
    assert r["cmp_check"]["status"] == "above_target" and r["buy_price"] is None and r["quantity"] == int(100000 // 121.5)
    reset(ltp=112.0); r = await server._analyze_one(server._server_url("bhavcopy"), server.StockAllocation(symbol="TCS", allocation=100000), dt.date.today().isoformat())
    assert r["cmp_check"]["status"] == "below_stop" and r["buy_price"] is None
    reset(fail=True); r = await server._analyze_one(server._server_url("bhavcopy"), server.StockAllocation(symbol="TCS", allocation=100000), dt.date.today().isoformat())
    assert not r.get("error") and (r["latest_price"], r["cmp_source"]) == (ENTRY, "eod") and r["cmp_note"], r            # feed down: analysis still works

    # ---- the assistant's analyze_stock reports CMP and last close separately
    reset(); a = await agent_tools.analyze_stock("TCS")
    assert (a["cmp"], a["cmp_source"], a["last_close"], a["last_close_date"]) == (116.5, "live", ENTRY, DAYS[-1]) and a["cmp_check"]["status"] == "ok" and "last_price" not in a, a

    # ---- suggestions: sized at the CMP; setups already past stop/target are skipped
    async def call_tool(url, tool, args): return OK([{"symbol": s} for s in ("AAA", "BBB", "CCC", "DDD")])
    async def fetch_history(url, sym, months): return RECORDS
    prices = {"AAA": 116.5, "BBB": 122.0, "CCC": 111.0, "DDD": None}
    async def lookup(sym, eod_close, eod_date):
        if prices[sym] is None: raise RuntimeError("feed down")
        return {"cmp": prices[sym], "source": "live", "as_of": "12 Oct 2026 10:00:27", "change_pct": 0.5, "note": None}
    res = await recommend.recommend("u", [], 100000, fetch_history, call_tool, price_lookup=lookup)
    assert [p["symbol"] for p in res["picks"]] == ["AAA", "DDD"], res["picks"]
    assert {x["symbol"]: x["reason"] for x in res["skipped_by_price"]} == {"BBB": "price has already reached its target", "CCC": "price is already below its stop"}
    a_, d_ = res["picks"]
    assert a_["quantity"] == int(50000 // 116.5) and a_["invested"] == round(a_["quantity"] * 116.5, 2) and a_["cmp_check"]["status"] == "ok"
    assert d_["cmp"] is None and d_["quantity"] == int(50000 // ENTRY)                                               # no live price: falls back to the entry
    old = await recommend.recommend("u", [], 100000, fetch_history, call_tool)                                       # without a lookup nothing changes
    assert len(old["picks"]) == 4 and old["skipped_by_price"] == [] and old["picks"][0]["quantity"] == int(25000 // ENTRY)
    print("cmp unit tests passed")

def endpoints():
    c = TestClient(server.app)
    # ---- /api/cmp
    reset(); r = c.get("/api/cmp?symbols=tcs,INFY").json()
    assert r["market"]["state"] == "open" and r["prices"]["TCS"]["cmp"] == 116.5 and set(r["prices"]) == {"TCS", "INFY"}
    assert c.get("/api/cmp?symbols=bad%20sym!").status_code == 400 and c.get("/api/cmp?symbols=").status_code == 400
    reset(fail=True); assert c.get("/api/cmp?symbols=TCS").json()["prices"]["TCS"]["source"] == "none"            # no EOD to fall back to here

    # ---- /api/analyze: sizing, funding at the buy price, 52-week position
    reset(); body = {"stocks": [{"symbol": "TCS", "allocation": 100000}], "mode": "equal", "total_capital": 100000, "use_mtf": False}
    r = c.post("/api/analyze", json=body).json()["results"][0]
    f = r["funding"]; assert r["quantity"] == int(100000 // 116.5) and f["position"] == round(r["quantity"] * 116.5, 2), (r["quantity"], f["position"])
    assert f["outcomes"]["entry"] == 116.5 and f["outcomes"]["gross_rr"] == round((TARGET - 116.5) / (116.5 - STOP), 2), f["outcomes"]   # costs computed from what you'd pay
    reset(ltp=121.5); r = c.post("/api/analyze", json=body).json()["results"][0]
    assert r["cmp_check"]["status"] == "above_target" and r["funding"]["outcomes"]["entry"] == ENTRY                 # setup void: shown on the signal's own entry

    # ---- /api/track/evaluate: open trades shown at the CMP with stop/target alerts
    e = {"id": "abc12345", "symbol": "TCS", "as_of": DAYS[-30], "entry": CLOSES[-30], "stop_loss": CLOSES[-30] * 0.5, "target": CLOSES[-30] * 3, "source": "user"}
    def ev(**kw): reset(**kw); return c.post("/api/track/evaluate", json={"entries": [e], "max_hold": 120}).json()["entries"][0]
    row = ev(ltp=150.0); assert row["status"] == "open" and row["current_price"] == 150.0 and row["price_source"] == "live" and row["live_alert"] is None
    assert row["return_pct"] == round((150.0 / CLOSES[-30] - 1) * 100, 2)
    assert ev(ltp=CLOSES[-30] * 0.4)["live_alert"] == "below_stop" and ev(ltp=CLOSES[-30] * 3.1)["live_alert"] == "at_target"
    row = ev(market="closed"); assert row["price_source"] == "last_trade"
    row = ev(fail=True); assert row["price_source"] == "eod" and row["current_price"] == CLOSES[-1] and "live_alert" not in row    # feed down: last close, no alert
    print("cmp endpoint tests passed")

asyncio.run(unit()); endpoints()
