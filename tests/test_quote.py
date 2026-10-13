import os, tempfile
os.environ.update(DASHBOARD_DATA_DIR=tempfile.mkdtemp()); os.environ.pop("GOOGLE_CLIENT_ID", None)
import server

# The CM server's real response for one stock: the quote is nested under "stock".
raw = {"updatedAt": "2026-10-12T04:30:27.596522429Z",
       "stock": {"type": "CM", "symbol": "JTLIND", "series": "EQ", "openPrice": 84.41, "highPrice": 86.8, "lowPrice": 84.41,
                 "preClosePrice": 83.98, "lastTradedPrice": 86.17, "indicativeClosePrice": 0, "change": 2.19, "perChange": 2.61,
                 "volume": 671866, "value": 5.760579084, "fiftyTwoWeekHigh": 94, "fiftyTwoWeekLow": 40.25, "perChange30d": -2.83,
                 "latestTimestamp": "2026-10-12 10:00:27"}}
q = server._normalise_quote("JTLIND", raw)
assert (q["ltp"], q["open"], q["high"], q["low"], q["prev_close"]) == (86.17, 84.41, 86.8, 84.41, 83.98), q
assert (q["change"], q["pct_change"], q["volume"], q["high_52w"], q["low_52w"], q["series"]) == (2.19, 2.61, 671866, 94, 40.25, "EQ"), q
assert q["updated"] == "12 Oct 2026 10:00:27", q["updated"]              # the quote's own time, not the wrapper's

# other shapes keep working, and an unknown wrapper name is found too
flat = raw["stock"]
assert server._normalise_quote("X", flat)["ltp"] == 86.17
assert server._normalise_quote("X", {"data": flat})["ltp"] == 86.17
assert server._normalise_quote("X", [{"quote": flat}])["ltp"] == 86.17
assert server._normalise_quote("X", {"updatedAt": "t", "equity": flat})["ltp"] == 86.17
# a quote with no live price shows a dash; it never borrows a close
q = server._normalise_quote("X", {"stock": {"symbol": "X", "preClosePrice": 50.0, "closePrice": 50.0}})
assert q["ltp"] is None and q["prev_close"] == 50.0, q
print("quote parsing tests passed")
