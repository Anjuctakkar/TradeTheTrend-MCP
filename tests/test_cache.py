import os, tempfile, asyncio, json, time, datetime as dt
from collections import Counter
tmp = tempfile.mkdtemp()
os.environ.update(DASHBOARD_DATA_DIR=tmp, GOOGLE_CLIENT_ID="test-client.apps.googleusercontent.com", DASHBOARD_SECRET="x"*40,
                  ADMIN_EMAILS="boss@example.com", ALLOWED_EMAILS="friend@example.com", ANTHROPIC_API_KEY="sk-ant-test")
import httpx
import auth, db, nse_cache, server
from nse_cache import NseCache, IST

db.init_db()
OK = lambda content: {"is_error": False, "content": content}
HIST = ("get_stock_history", {"symbol": "TCS", "months": 3, "endDate": "today"})

def at(h, m, day=12):  # 2026-10-12 is a Monday, 2026-10-17 a Saturday
    return dt.datetime(2026, 10, day, h, m, tzinfo=IST)

async def unit():
    # ---- lifetimes
    assert nse_cache._eod_ttl({}, at(15, 0)) == 3600                    # capped by the 16:00 boundary
    assert nse_cache._eod_ttl({}, at(17, 0)) == nse_cache.EOD_TTL_PUBLISH
    assert nse_cache._eod_ttl({}, at(21, 55)) == 300                    # capped by the 22:00 boundary
    assert nse_cache._eod_ttl({}, at(22, 10)) == nse_cache.EOD_TTL_IDLE
    assert nse_cache._eod_ttl({}, at(17, 0, day=17)) == 18000           # Saturday: idle TTL, still capped by 22:00
    assert nse_cache._history_ttl({"endDate": "2026-01-01"}, at(17, 0)) == 86400
    assert nse_cache._history_ttl({"endDate": "today"}, at(17, 0)) == nse_cache.EOD_TTL_PUBLISH
    k = nse_cache.make_key
    assert k("t", "u", {"a": 1, "b": 2}) == k("t", "u", {"b": 2, "a": 1}) and k("t", "u", {"a": 1}) != k("t", "u", {"a": 2}) and k("t", "u", {}) != k("t2", "u", {})

    c, n = NseCache(), Counter()
    def fetcher(content=None, boom=False, delay=0.0, name="f"):
        async def f():
            n[name] += 1
            if delay: await asyncio.sleep(delay)
            if boom: raise RuntimeError("NSE timeout")
            return OK(content if content is not None else {"data": [1, 2, 3]})
        return f

    # ---- hit / miss / separate arguments
    a = await c.get_or_fetch(*HIST[:1], "u", HIST[1], fetcher())
    b = await c.get_or_fetch(HIST[0], "u", HIST[1], fetcher())
    assert a == b and n["f"] == 1 and c.stats["hits"] == 1 and c.stats["misses"] == 1
    await c.get_or_fetch(HIST[0], "u", {**HIST[1], "symbol": "INFY"}, fetcher())
    assert n["f"] == 2
    # ---- callers can't corrupt the cache
    a["content"]["data"].append(99); a["content"]["x"] = 1
    assert (await c.get_or_fetch(HIST[0], "u", HIST[1], fetcher()))["content"] == {"data": [1, 2, 3]}
    # ---- survives a restart / second instance (database layer)
    c._l1.clear(); n.clear()
    assert (await c.get_or_fetch(HIST[0], "u", HIST[1], fetcher()))["content"]["data"] == [1, 2, 3] and not n
    other = NseCache(); assert (await other.get_or_fetch(HIST[0], "u", HIST[1], fetcher()))["content"]["data"] == [1, 2, 3] and not n
    # ---- unknown tools bypass the cache
    for _ in range(2): await c.get_or_fetch("some_other_tool", "u", {}, fetcher(name="o"))
    assert n["o"] == 2
    # ---- errors and empty answers are not cached
    c2 = NseCache(); n.clear()
    for _ in range(2):
        r = await c2.get_or_fetch("get_ltp_by_date", "u", {"symbol": "E", "date": "d"}, lambda: _ret({"is_error": True, "content": "bad"}, n))
    assert n["r"] == 2
    for _ in range(2): await c2.get_or_fetch("get_ltp_by_date", "u", {"symbol": "Z", "date": "d"}, lambda: _ret({"is_error": False, "content": None}, n))
    assert n["r"] == 4
    for _ in range(2):
        try: await c2.get_or_fetch("get_ltp_by_date", "u", {"symbol": "Q", "date": "d"}, fetcher(boom=True, name="b")); assert False
        except RuntimeError: pass
    assert n["b"] == 2
    # ---- ten simultaneous requests -> one NSE call
    c3 = NseCache(); n.clear()
    rs = await asyncio.gather(*(c3.get_or_fetch("get_corporate_actions", "u", {"symbol": "S"}, fetcher(delay=0.2, name="s")) for _ in range(10)))
    assert n["s"] == 1 and c3.stats["coalesced"] == 9 and all(r == rs[0] for r in rs)
    # ---- NSE failing: serve the expired copy, flagged stale
    c4 = NseCache(); n.clear()
    await c4.get_or_fetch("get_ltp_by_date", "u", {"symbol": "ST", "date": "d"}, fetcher())
    for key, v in list(c4._l1.items()): c4._l1[key] = (0, v[1], v[2])          # expire it (still inside the stale window)
    r = await c4.get_or_fetch("get_ltp_by_date", "u", {"symbol": "ST", "date": "d"}, fetcher(boom=True, name="x"))
    assert r["stale"] is True and r["content"]["data"] == [1, 2, 3] and c4.stats["stale_served"] == 1
    # ---- live quotes: short life, never stale
    nse_cache.LIVE_TTL = 0.15; c5 = NseCache(); n.clear()
    args = {"symbol": "TCS"}
    await c5.get_or_fetch("cm_get_stock_quote", "u", args, fetcher(name="q")); await c5.get_or_fetch("cm_get_stock_quote", "u", args, fetcher(name="q"))
    assert n["q"] == 1
    await asyncio.sleep(0.25)
    try: await c5.get_or_fetch("cm_get_stock_quote", "u", args, fetcher(boom=True, name="q")); assert False
    except RuntimeError: pass
    await c5.get_or_fetch("cm_get_stock_quote", "u", args, fetcher(name="q")); assert n["q"] == 3
    nse_cache.LIVE_TTL = 30
    # ---- switched off
    nse_cache.ENABLED = False; n.clear()
    for _ in range(2): await NseCache().get_or_fetch(HIST[0], "u", HIST[1], fetcher(name="off"))
    assert n["off"] == 2; nse_cache.ENABLED = True
    print("cache unit tests passed")

async def _ret(v, n):
    n["r"] += 1
    return v

# ------------------------------------------------------------------ through the real app, two users
raw_calls, running, peak = Counter(), [0], [0]
async def fake_raw(url, tool, args):
    raw_calls[(tool, json.dumps(args, sort_keys=True))] += 1
    if tool == "cm_get_stock_quote":
        return OK({"symbol": args["symbol"], "ltp": 101.5, "prev_price": 100.0, "latestTimestamp": dt.datetime.now(IST).strftime("%d-%b-%Y %H:%M:%S")})
    if tool == "get_bulk_quote":
        return OK({"quotes": [{"symbol": s, "close": 100, "date": "2026-10-09"} for s in args["symbols"]]})
    if tool in ("cm_get_data_status", "cm_get_allstocks_status"):
        return OK({"lastCrawled": "2026-10-09 15:30:00"})
    if tool == "get_top_by_volume":
        running[0] += 1; peak[0] = max(peak[0], running[0]); await asyncio.sleep(0.3); running[0] -= 1
        return OK([{"symbol": s} for s in ("AAA", "BBB", "CCC")])
    if tool == "get_stock_history":
        return OK({"data": [{"date": f"2026-0{1 + i // 28}-{1 + i % 28:02d}", "close": 100 + i * 0.1} for i in range(70)], "next_end_date": None})
    if tool == "get_corporate_actions":
        return OK([{"note": "none"}])
    return OK({})
server._call_tool_raw = fake_raw

async def endpoints():
    server.NSE_HEAVY_JOBS, server._heavy = 1, None
    async def login(email):
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test", follow_redirects=False)
        auth._verify_google = lambda cred: {"email": email, "email_verified": True, "sub": "sub-" + email, "name": email, "picture": None}
        assert (await c.post("/api/auth/google", json={"credential": "ok"})).status_code == 200
        return c
    A, B = await login("boss@example.com"), await login("friend@example.com")
    assert (await A.get("/health")).json() == {"ok": True}

    # two users watching the same stock: NSE is asked once
    ra, rb = await asyncio.gather(A.get("/api/live?symbols=TCS"), B.get("/api/live?symbols=TCS"))
    assert ra.status_code == rb.status_code == 200 and ra.json()["quotes"][0]["ltp"] == 101.5
    assert raw_calls[("cm_get_stock_quote", json.dumps({"symbol": "TCS"}))] == 1, raw_calls
    assert (await B.get("/api/live?symbols=TCS")).status_code == 200 and raw_calls[("cm_get_stock_quote", json.dumps({"symbol": "TCS"}))] == 1

    # the admin tool explorer always asks NSE
    before = sum(raw_calls.values())
    for _ in range(2): assert (await A.post("/api/call", json={"server": "cm", "tool": "cm_get_stock_quote", "arguments": {"symbol": "TCS"}})).status_code == 200
    assert sum(raw_calls.values()) == before + 2

    # two users scanning at once: scans queue (1 at a time here) and the second reuses the first's data
    t0 = time.time()
    r1, r2 = await asyncio.gather(A.post("/api/recommend", json={"budget": 100000, "existing": []}), B.post("/api/recommend", json={"budget": 100000, "existing": []}))
    assert r1.status_code == r2.status_code == 200, (r1.text, r2.text)
    assert peak[0] == 1, peak                                          # never two scans at once
    assert raw_calls[("get_top_by_volume", json.dumps({"date": dt.date.today().isoformat(), "n": 30, "sortBy": "value"}, sort_keys=True))] == 1
    hist = [v for (t, a), v in raw_calls.items() if t == "get_stock_history" and '"AAA"' in a and "today" in a]
    assert hist == [1], hist                                           # AAA's history fetched once for both users

    # the same user can't start a second scan while one runs
    server._call_tool_raw = fake_raw
    nse_cache.CACHE.clear("get_top_by_volume")
    s1 = asyncio.create_task(A.post("/api/recommend", json={"budget": 1, "existing": []})); await asyncio.sleep(0.1)
    s2 = await A.post("/api/recommend", json={"budget": 1, "existing": []}); assert s2.status_code == 409, s2.text
    assert (await s1).status_code == 200

    # admin cache report and clear
    rep = (await A.get("/api/admin/cache")).json()
    assert rep["enabled"] and rep["since_start"]["hits"] > 0 and "get_stock_history" in rep["by_tool"] and rep["hit_rate_pct"] > 0
    assert (await B.get("/api/admin/cache")).status_code == 403 and (await B.delete("/api/admin/cache")).status_code == 403
    removed = (await A.delete("/api/admin/cache")).json()["removed"]; assert removed > 0
    assert (await A.get("/api/admin/cache")).json()["entries_in_db"] == {}
    print("cache endpoint tests passed")

asyncio.run(unit())
asyncio.run(endpoints())
