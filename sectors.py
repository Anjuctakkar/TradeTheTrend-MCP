"""
Sector momentum: which parts of the market are gaining strength.

Classification follows NSE's macro sectors (Commodities, Consumer Discretionary, Energy, FMCG, Financial Services,
Healthcare, Industrials, Information Technology, Services, Telecommunication, Utilities, Diversified), each split into
its industry groups (for example Financial Services: Banks, Financial Institutions, Capital Markets, Insurance).
Every industry group holds a short basket of stocks tiered large, mid and small. A macro sector and each of its
industry groups is an equal-weight index (split-adjusted closes, rebalanced daily, base 100), indexed three ways:
all stocks, large caps only, and mid and small caps only, so you can see whether strength is broad or only in the big names. These are proxies made from the app's own price data, NOT official NSE sector indices, and the cap
tiers are approximate and drift over time.

Add to or change the baskets with a sector_map.json in the data folder (it is merged with the built-in lists; add "_mode": "replace" to use only your file). A sector is {industry: tiers}, or just tiers, or a plain list
(treated as large caps):
  {"Defence": {"Defence": {"large": ["HAL", "BEL"], "mid": ["BDL"], "small": ["DATAPATTNS"]}}, "Quick": ["TCS", "INFY"]}

Prices: history is end-of-day (adjusted). Where the live feed has a current price from a later session, it is added as the newest
point, so 1D = latest traded price (LTP) against the previous close, and the newest weekly/monthly bar is partial.
Views: daily closes, weekly (last close of each week), monthly (last close of each month).
Momentum rules (mechanical, not advice):
  score  = 0.4 x rank of 1-month return + 0.3 x 3-month + 0.2 x 1-week + 0.1 x breadth (percentile among the sectors loaded)
  status = gaining: 1M and 1W both positive, index above its 20-day average, and at least half the stocks above their 50-day average
           losing:  1M negative and index below its 20-day average
           mixed:   anything else
"""
import datetime as dt
import json
from typing import Any, Optional

import backtest
import config
import sr_model
import technical

def _t(large: str = "", mid: str = "", small: str = "") -> dict[str, list[str]]:
    return {k: v.split() for k, v in (("large", large), ("mid", mid), ("small", small)) if v.split()}


# Industry groups follow the NSE macro-sector classification supplied for this project. The stocks in each group and
# their large/mid/small tiers are approximate (market caps drift) and are meant to be edited in sector_map.json.
DEFAULT_SECTORS: dict[str, dict[str, dict[str, list[str]]]] = {
    "Commodities": {
        "Chemicals": _t("PIDILITIND SRF", "DEEPAKNTR AARTIIND NAVINFLUOR ATUL", "NOCIL VINATIORGA"),
        "Fertilizers & Agri inputs": _t("UPL", "PIIND COROMANDEL CHAMBLFERT", "RCF GSFC"),
        "Construction Materials": _t("ULTRACEMCO AMBUJACEM SHREECEM GRASIM", "DALBHARAT JKCEMENT RAMCOCEM", "JKLAKSHMI STARCEMENT"),
        "Metals & Mining": _t("TATASTEEL JSWSTEEL HINDALCO VEDL COALINDIA NMDC", "SAIL NATIONALUM HINDZINC JINDALSTEL", "MOIL GPIL"),
        "Forest Products": _t("", "", "JKPAPER TNPL GREENPLY"),
    },
    "Consumer Discretionary": {
        "Automobiles & Auto Components": _t("MARUTI M&M BAJAJ-AUTO EICHERMOT HEROMOTOCO TVSMOTOR", "ASHOKLEY BHARATFORG BALKRISIND", "GABRIEL SUPRAJIT JAMNAAUTO"),
        "Consumer Durables": _t("HAVELLS", "VOLTAS BLUESTARCO CROMPTON WHIRLPOOL KAJARIACER", "SYMPHONY VGUARD"),
        "Textiles": _t("", "KPRMILL RAYMOND VTL WELSPUNLIV", "TRIDENT ARVIND"),
        "Apparel, Footwear & Accessories": _t("TITAN", "PAGEIND BATAINDIA RELAXO KALYANKJIL", "SENCO CAMPUS"),
        "Media, Entertainment & Publication": _t("", "SUNTV PVRINOX", "ZEEL SAREGAMA TV18BRDCST DBCORP"),
        "Realty": _t("DLF LODHA", "GODREJPROP OBEROIRLTY PRESTIGE PHOENIXLTD", "BRIGADE SOBHA SUNTECK"),
        "Consumer Services": _t("TRENT DMART", "INDHOTEL JUBLFOOD DEVYANI", "WESTLIFE LEMONTREE CHALET"),
    },
    "Energy": {
        "Oil, Gas & Consumable Fuels": _t("RELIANCE ONGC BPCL IOC GAIL", "PETRONET IGL OIL HINDPETRO MGL", "GSPL CASTROLIND GUJGASLTD MRPL"),
        "Alternative Energy": _t("ADANIGREEN", "SUZLON WAAREEENER", "INOXWIND"),
    },
    "FMCG": {
        "Agricultural Food Products": _t("NESTLEIND BRITANNIA TATACONSUM", "AWL BALRAMCHIN BIKAJI", "HERITGFOOD AVANTIFEED"),
        "Beverages": _t("VBL UNITDSPR", "RADICO UBL", "ABDL"),
        "Tobacco": _t("ITC", "GODFRYPHLP", "VSTIND"),
        "Personal Care & Household Products": _t("HINDUNILVR DABUR GODREJCP MARICO", "COLPAL PGHH EMAMILTD", "JYOTHYLAB BAJAJCON"),
    },
    "Financial Services": {
        "Banks": _t("HDFCBANK ICICIBANK AXISBANK KOTAKBANK SBIN INDUSINDBK", "FEDERALBNK IDFCFIRSTB BANDHANBNK PNB BANKBARODA", "CUB KARURVYSYA DCBBANK"),
        "Financial Institutions": _t("BAJFINANCE CHOLAFIN SHRIRAMFIN MUTHOOTFIN", "MANAPPURAM POONAWALLA LICHSGFIN PFC RECLTD", "CREDITACC AAVAS MASFIN"),
        "Capital Markets": _t("HDFCAMC BSE", "CDSL MCX CAMS ANGELONE", "KFINTECH ANANDRATHI"),
        "Insurance": _t("SBILIFE HDFCLIFE ICICIGI LICI", "ICICIPRULI STARHEALTH GICRE NIACL"),
    },
    "Healthcare": {
        "Pharmaceuticals & Biotechnology": _t("SUNPHARMA CIPLA DRREDDY DIVISLAB LUPIN ZYDUSLIFE", "AUROPHARMA ALKEM TORNTPHARM GLENMARK BIOCON", "GRANULES NATCOPHARM AJANTPHARM"),
        "Healthcare Services": _t("APOLLOHOSP MAXHEALTH", "FORTIS KIMS LALPATHLAB SYNGENE", "METROPOLIS THYROCARE RAINBOW"),
        "Medical Equipment & Supplies": _t("", "POLYMED", "BLISSGVS"),
    },
    "Industrials": {
        "Capital Goods": _t("SIEMENS ABB CUMMINSIND POLYCAB BHEL", "THERMAX CGPOWER KAYNES", "ELGIEQUIP TRITURBINE KIRLOSENG GRINDWELL"),
        "Construction & Engineering": _t("LT", "KEC KALPATPOWR NCC RVNL", "PNCINFRA HGINFRA"),
        "Defense & Aerospace": _t("HAL BEL", "BDL MAZDOCK SOLARINDS", "DATAPATTNS MIDHANI"),
        "Industrial Products": _t("", "ASTRAL SUPREMEIND APLAPOLLO AIAENG", "FINPIPE KSB"),
    },
    "Information Technology": {
        "IT Software & Services": _t("TCS INFY HCLTECH WIPRO TECHM LTIM", "PERSISTENT COFORGE MPHASIS OFSS", "ZENSARTECH BSOFT SONATSOFTW"),
        "IT Hardware": _t("", "NETWEB", "REDINGTON"),
        "BPO/KPO Services": _t("", "FIRSTSOURCE ECLERX", "HGS"),
    },
    "Services": {
        "Transport Services": _t("INDIGO ADANIPORTS", "CONCOR DELHIVERY IRCTC GMRAIRPORT", "BLUEDART TCI VRLLOG GESHIP"),
        "Commercial Services": _t("", "QUESS TEAMLEASE", "SIS CMSINFO"),
    },
    "Telecommunication": {
        "Telecom Services": _t("BHARTIARTL INDUSTOWER", "IDEA TATACOMM"),
        "Telecom Equipment": _t("", "TEJASNET", "HFCL STLTECH ITI"),
    },
    "Utilities": {
        "Power": _t("NTPC POWERGRID TATAPOWER ADANIPOWER", "NHPC TORNTPOWER JSWENERGY CESC", "SJVN NLCINDIA"),
    },
    "Diversified": {
        "Diversified": _t("", "3MINDIA GODREJIND", "DCMSHRIRAM BALMERLAWRI"),
    },
}
TIERS = ("large", "mid", "small")
CAPS = ("all", "large", "midsmall")                      # the three ways each sector is indexed
_CAP_TIERS = {"all": TIERS, "large": ("large",), "midsmall": ("mid", "small")}
TFS = ("daily", "weekly", "monthly")
WEIGHTS = {"m1": 0.4, "m3": 0.3, "w1": 0.2, "breadth50": 0.1}


def _ok_symbol(s: Any) -> bool:
    return isinstance(s, str) and 1 <= len(s.strip()) <= 20 and all(c.isalnum() or c in "-&_" for c in s.strip())


def _clean(v: Any) -> list[str]:
    return list(dict.fromkeys(s.strip().upper() for s in v if _ok_symbol(s)))[:15] if isinstance(v, list) else []


def _tiers(v: Any) -> dict[str, list[str]]:
    """A list (all large caps) or {large, mid, small} -> clean tiers, empty ones dropped."""
    raw = {"large": v} if isinstance(v, list) else v if isinstance(v, dict) else {}
    return {t: c for t in TIERS if (c := _clean(raw.get(t)))}


def _merge(base: dict[str, dict[str, dict[str, list[str]]]], extra: dict[str, dict[str, dict[str, list[str]]]]) -> dict[str, dict[str, dict[str, list[str]]]]:
    """Add the file's stocks to the built-in lists (sector and industry names match without regard to case)."""
    out = {s: {i: {t: list(l) for t, l in tiers.items()} for i, tiers in inds.items()} for s, inds in base.items()}
    low_s = {s.lower(): s for s in out}
    for sec, inds in extra.items():
        target = out.setdefault(low_s.get(sec.lower(), sec), {})
        low_i = {i.lower(): i for i in target}
        for ind, tiers in inds.items():
            cur = target.setdefault(low_i.get(ind.lower(), ind), {})
            for t, syms in tiers.items():
                cur[t] = list(dict.fromkeys(cur.get(t, []) + syms))
    return out


def _map_file():
    return config.DATA_DIR / "sector_map.json"


def load_sectors() -> dict[str, dict[str, dict[str, list[str]]]]:
    """{sector: {industry: {tier: [symbols]}}}. The built-in classification, plus whatever sector_map.json adds to it
    (or only the file, if it contains "_mode": "replace"). A missing or unreadable file means the built-ins alone."""
    f = _map_file()
    if f.exists():
        try:
            raw = json.loads(f.read_text(encoding="utf-8-sig"))
            out: dict[str, dict[str, dict[str, list[str]]]] = {}
            for k, v in raw.items():
                if k == "_mode":
                    continue
                if isinstance(v, dict) and not any(t in v for t in TIERS):      # {industry: tiers}
                    inds = {str(i).strip()[:40]: _tiers(x) for i, x in v.items()}
                else:                                                            # tiers or a plain list: one industry
                    inds = {str(k).strip()[:40]: _tiers(v)}
                inds = {i: t for i, t in inds.items() if i and t}
                if str(k).strip() and inds:
                    out[str(k).strip()[:30]] = inds
            if out:
                return out if raw.get("_mode") == "replace" else _merge(DEFAULT_SECTORS, out)
        except (ValueError, OSError, AttributeError):
            pass
    return DEFAULT_SECTORS


def map_status() -> dict[str, Any]:
    """Where sector_map.json is looked for, and whether it was found and readable (for the Sectors tab)."""
    f = _map_file()
    st: dict[str, Any] = {"path": str(f), "exists": f.exists(), "mode": None, "entries": 0, "problem": None}
    if st["exists"]:
        try:
            raw = json.loads(f.read_text(encoding="utf-8-sig"))
            st["mode"] = "replace" if raw.get("_mode") == "replace" else "merge"
            st["entries"] = len([k for k in raw if k != "_mode"])
        except (ValueError, OSError, AttributeError) as exc:
            st["problem"] = f"Couldn't read it ({exc}). The built-in lists are being used."
    return st


def symbols_of(tiers: dict[str, list[str]]) -> list[str]:
    return list(dict.fromkeys(s for t in TIERS for s in tiers.get(t, [])))


def all_symbols(sector: dict[str, dict[str, list[str]]]) -> list[str]:
    return list(dict.fromkeys(s for tiers in sector.values() for s in symbols_of(tiers)))


def tier_map(tiers: dict[str, list[str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in TIERS:
        for s in tiers.get(t, []):
            out.setdefault(s, t)
    return out


def equal_weight_index(closes: dict[str, dict[str, float]]) -> tuple[list[str], list[float]]:
    """closes: {symbol: {date: close}}. Equal-weight, rebalanced daily: each day the index moves by the average
    return of the stocks that traded that day and the day before. Base 100."""
    dates = sorted({d for s in closes.values() for d in s})
    idx, prev, out_v = 100.0, {}, []
    for d in dates:
        rets = []
        for sym, s in closes.items():
            c = s.get(d)
            if c is None:
                continue
            if prev.get(sym):
                rets.append(c / prev[sym] - 1)
            prev[sym] = c
        if rets:
            idx *= 1 + sum(rets) / len(rets)
        out_v.append(round(idx, 4))
    return dates, out_v


def resample(dates: list[str], values: list[float], tf: str) -> dict[str, list]:
    """Last close of each week (ISO week) or month; 'daily' returns the series as is."""
    if tf == "daily":
        return {"dates": dates, "values": values}
    key = (lambda d: dt.date.fromisoformat(d).isocalendar()[:2]) if tf == "weekly" else (lambda d: d[:7])
    od: list[str] = []
    ov: list[float] = []
    last = None
    for d, v in zip(dates, values):
        k = key(d)
        if k == last:
            od[-1], ov[-1] = d, v
        else:
            od.append(d); ov.append(v); last = k
    return {"dates": od, "values": ov}


def pct(values: list[float], n: int) -> Optional[float]:
    return round((values[-1] / values[-1 - n] - 1) * 100, 2) if len(values) > n and values[-1 - n] else None


def metrics(dates: list[str], values: list[float], breadth: Optional[int]) -> dict[str, Any]:
    sma20 = sum(values[-20:]) / 20 if len(values) >= 20 else None
    return {"d1": pct(values, 1), "w1": pct(values, 5), "m1": pct(values, 21), "m3": pct(values, 63), "m6": pct(values, 125),
            "above_sma20": None if sma20 is None else values[-1] > sma20, "breadth50": breadth, "as_of": dates[-1]}


def status(m: dict[str, Any]) -> str:
    if (m.get("m1") or 0) > 0 and (m.get("w1") or 0) > 0 and m.get("above_sma20") and (m.get("breadth50") or 0) >= 50:
        return "gaining"
    if (m.get("m1") or 0) < 0 and m.get("above_sma20") is False:
        return "losing"
    return "mixed"


def live_bar(info: Optional[dict[str, Any]], last_date: str, last_close: float) -> Optional[dict[str, Any]]:
    """How a current price (from server.get_cmp) joins the end-of-day history. None = don't use it.
    bar=True: the quote is from a later session than the history, so it becomes the newest point (and 1D is
    that price against the previous close). bar=False: the history already holds that session; the quote is only shown."""
    if not info or info.get("cmp") is None or info.get("source") not in ("live", "last_trade"):
        return None
    qd = str(info.get("as_of_iso") or "")[:10]
    if not qd or qd < last_date:
        return None                                    # undated, or older than the history: not a current price
    bar = qd > last_date
    prev = info.get("prev_close")
    if bar and prev and last_close and abs(prev / last_close - 1) > 0.10:
        return None                                    # previous close disagrees with the adjusted history (corporate action?)
    return {"cmp": float(info["cmp"]), "date": qd, "bar": bar, "source": info["source"], "as_of": info.get("as_of_iso")}


def stock_row(sym: str, p: dict[str, list], live: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """One constituent. Setups use end-of-day bars (that is how the rules are defined); the returns, price and
    50-day check use the latest traded price when there is one, so 1D is LTP against the previous close."""
    c = p["c"]
    s20, s50, rsi = technical.sma(c, 20), technical.sma(c, 50), technical.rsi(c, 14)
    t = technical.generate_trade_signal(c, s20, s50, rsi, *technical.macd(c))
    s = sr_model.generate_sr_signal(p["o"], p["h"], p["l"], p["c"])
    px = c + [live["cmp"]] if live and live["bar"] else c
    return {"symbol": sym, "close": round(live["cmp"] if live else c[-1], 2), "date": live["date"] if live else p["dates"][-1],
            "d1": pct(px, 1), "w1": pct(px, 5), "m1": pct(px, 21),
            "above_sma50": None if len(px) < 50 else px[-1] > sum(px[-50:]) / 50, "trend_long": t["signal"] == "LONG",
            "sr_long": s["signal"] == "LONG", "sr_setup": s.get("setup"),
            "ltp": live["cmp"] if live else None, "ltp_source": live["source"] if live else None, "ltp_as_of": live["as_of"] if live else None}


def _segments(closes: dict[str, dict[str, float]], stocks: list[dict[str, Any]], tiers: dict[str, str]) -> dict[str, Any]:
    """The all / large / mid+small indices for one group of stocks (a sector or an industry group)."""
    segments: dict[str, Any] = {}
    for cap in CAPS:
        syms = [s for s in closes if tiers.get(s) in _CAP_TIERS[cap]]
        if not syms:
            continue
        dates, vals = equal_weight_index({s: closes[s] for s in syms})
        have = [s for s in stocks if s["symbol"] in syms and s["above_sma50"] is not None]
        breadth = round(100 * sum(s["above_sma50"] for s in have) / len(have)) if have else None
        segments[cap] = {"metrics": metrics(dates, vals, breadth), "used": len(syms),
                         "total": sum(1 for t in tiers.values() if t in _CAP_TIERS[cap]),
                         "series": {tf: resample(dates, vals, tf) for tf in TFS}}
    return segments


def assemble(name: str, sector: dict[str, dict[str, list[str]]], records: dict[str, list[dict[str, Any]]],
             errors: dict[str, str], live: Optional[dict[str, dict[str, Any]]] = None) -> dict[str, Any]:
    """Build a macro sector and each of its industry groups from the stocks' price records.
    live: {symbol: quote info from get_cmp}; where usable it becomes the newest price point."""
    sector_tiers: dict[str, str] = {}
    industry_of: dict[str, str] = {}
    for ind, tiers in sector.items():
        for s, t in tier_map(tiers).items():
            sector_tiers.setdefault(s, t)
            industry_of.setdefault(s, ind)
    closes: dict[str, dict[str, float]] = {}
    stocks: list[dict[str, Any]] = []
    history_to = ""
    for sym, recs in records.items():
        p = backtest.prepare(recs)
        if not p["c"]:
            errors[sym] = "no usable prices"
            continue
        last_date = p["dates"][-1][:10]
        history_to = max(history_to, last_date)
        lb = live_bar((live or {}).get(sym), last_date, p["c"][-1])
        by_date = dict(zip([d[:10] for d in p["dates"]], p["c"]))
        if lb and lb["bar"]:
            by_date[lb["date"]] = lb["cmp"]
        closes[sym] = by_date
        stocks.append({**stock_row(sym, p, lb), "cap": sector_tiers.get(sym, "large"), "industry": industry_of.get(sym, "")})
    if not closes:
        raise RuntimeError("No price data came back for any stock in this sector.")
    segments = _segments(closes, stocks, sector_tiers)
    industries: dict[str, Any] = {}
    for ind, tiers in sector.items():
        seg = _segments({s: c for s, c in closes.items() if s in tier_map(tiers)}, stocks, tier_map(tiers))
        if seg:
            industries[ind] = {"segments": seg, "symbols": symbols_of(tiers)}
    srcs = [s["ltp_source"] for s in stocks if s["ltp_source"]]
    live_meta = {"with_ltp": len(srcs), "total": len(closes), "live": srcs.count("live"), "last_trade": srcs.count("last_trade"),
                 "as_of": max((s["ltp_as_of"] for s in stocks if s["ltp_as_of"]), default=None)}
    return {"sector": name, "symbols": all_symbols(sector), "used": len(closes), "errors": errors,
            "as_of": segments["all"]["metrics"]["as_of"], "history_to": history_to, "live": live_meta,
            "segments": segments, "industries": industries,
            "stocks": sorted(stocks, key=lambda s: -(s["m1"] if s["m1"] is not None else -999))}


def rank(loaded: dict[str, dict[str, Any]], cap: str = "all") -> list[dict[str, Any]]:
    """Score and label every loaded group for one cap view (percentile ranks are among the groups passed in)."""
    segs = {n: d["segments"][cap] for n, d in loaded.items() if cap in d.get("segments", {})}

    def prank(key: str) -> dict[str, float]:
        ok = sorted((g["metrics"][key], n) for n, g in segs.items() if g["metrics"].get(key) is not None)
        return {n: (100 * i / (len(ok) - 1) if len(ok) > 1 else 50.0) for i, (_, n) in enumerate(ok)}
    ranks = {k: prank(k) for k in WEIGHTS}
    rows = [{"name": n, **g["metrics"], "score": round(sum(w * ranks[k].get(n, 50.0) for k, w in WEIGHTS.items())),
             "status": status(g["metrics"]), "used": g["used"], "total": g["total"]} for n, g in segs.items()]
    return sorted(rows, key=lambda r: -r["score"])


def rank_industries(loaded: dict[str, dict[str, Any]], cap: str = "all") -> list[dict[str, Any]]:
    """Every industry group of every loaded sector, ranked against each other. Rows carry sector and industry."""
    flat = {f"{s}|{i}": {"segments": g["segments"]} for s, d in loaded.items() for i, g in d.get("industries", {}).items()}
    rows = rank(flat, cap)
    for r in rows:
        r["sector"], r["industry"] = r.pop("name").split("|", 1)
    return rows


# ---------------------------------------------------------------------------------------------------------------
# Sector rotation (a simplified relative-rotation view; NOT the licensed JdK RRG formula)
#   RS            = sector index / benchmark index
#   RS-Ratio      = 100 x RS / its average over `win` periods         (above 100: stronger than its own recent norm)
#   RS-Momentum   = 100 x RS-Ratio / RS-Ratio `lag` periods ago        (above 100: relative strength is improving)
# Quadrants: leading (both >= 100), weakening (ratio >= 100, momentum < 100), lagging (both < 100),
#            improving (ratio < 100, momentum >= 100).
# Early movers: improving with RS-Ratio still rising, or just stepped from improving into leading.
# Sectors are measured against the equal-weight of the sectors loaded (not the Nifty 50); industry groups against
# their own sector. The newest point includes live prices, so a weekly or monthly point is partial until the period ends.
# ---------------------------------------------------------------------------------------------------------------
ROT = {"daily": {"win": 30, "lag": 5, "step": 5, "trail": 5},
       "weekly": {"win": 10, "lag": 3, "step": 1, "trail": 5},
       "monthly": {"win": 4, "lag": 1, "step": 1, "trail": 4}}
_SIGNAL = {"leading": "Leading: strong and still gaining", "weakening": "Weakening: still strong, momentum fading",
           "lagging": "Lagging: weak and losing ground", "improving": "Improving: weak, but relative strength is recovering"}


def quadrant(ratio: float, mom: float) -> str:
    return "leading" if ratio >= 100 and mom >= 100 else "weakening" if ratio >= 100 else "improving" if mom >= 100 else "lagging"


def _rs(series: dict[str, list], bench_dates: list[str], bench_vals: list[float], p: dict[str, int]):
    import bisect
    d: list[str] = []
    rs: list[float] = []
    for date, v in zip(series["dates"], series["values"]):
        k = bisect.bisect_right(bench_dates, date) - 1             # benchmark value on or before that date
        if k >= 0 and bench_vals[k]:
            d.append(date); rs.append(v / bench_vals[k])
    win, lag = p["win"], p["lag"]
    ratio = [None if i < win - 1 else 100 * rs[i] / (sum(rs[i - win + 1:i + 1]) / win) for i in range(len(rs))]
    mom = [100 * ratio[i] / ratio[i - lag] if i >= lag and ratio[i] is not None and ratio[i - lag] else None for i in range(len(rs))]
    return d, ratio, mom


def _item(name: str, series: dict[str, list], bd: list[str], bv: list[float], p: dict[str, int]) -> Optional[dict[str, Any]]:
    d, ratio, mom = _rs(series, bd, bv, p)
    ok = {i for i in range(len(d)) if ratio[i] is not None and mom[i] is not None}
    if len(ok) < 2:
        return None
    last = max(ok)
    idx = [i for i in sorted({last - k * p["step"] for k in range(p["trail"])}) if i in ok]
    if len(idx) < 2:
        idx = sorted(ok)[-2:]
    prev = idx[-2]
    q, qp = quadrant(ratio[last], mom[last]), quadrant(ratio[prev], mom[prev])
    signal = ("Entering Improving: early rotation in" if (q, qp) == ("improving", "lagging")
              else "Moved into Leading: strength confirmed" if (q, qp) == ("leading", "improving")
              else "Moved into Weakening: strength fading" if (q, qp) == ("weakening", "leading")
              else "Moved into Lagging" if (q, qp) == ("lagging", "weakening") else _SIGNAL[q])
    early = (q == "improving" and ratio[last] >= ratio[prev]) or (q == "leading" and qp == "improving")
    return {"name": name, "ratio": round(ratio[last], 2), "mom": round(mom[last], 2), "quadrant": q, "prev_quadrant": qp,
            "signal": signal, "early": early, "as_of": d[last],
            "trail": [{"date": d[i], "ratio": round(ratio[i], 2), "mom": round(mom[i], 2)} for i in idx]}


def rotation(loaded: dict[str, dict[str, Any]], tf: str = "weekly", cap: str = "all", level: str = "sectors", sector: str = "") -> dict[str, Any]:
    """Rotation points for every sector loaded (level='sectors') or the industry groups of one sector (level='industries')."""
    p = ROT[tf]
    out: dict[str, Any] = {"tf": tf, "cap": cap, "level": level, "params": p, "items": [], "warning": None, "benchmark": ""}
    if level == "industries":
        d = loaded.get(sector)
        if not d or cap not in d["segments"]:
            out["warning"] = "Load this sector (and pick a Stocks view it has) to see its industry groups."
            return out
        bench = d["segments"][cap]["series"][tf]
        groups = {i: g["segments"][cap]["series"][tf] for i, g in d.get("industries", {}).items() if cap in g["segments"]}
        out["benchmark"] = f"{sector} (equal-weight of its own stocks)"
        bd, bv = bench["dates"], bench["values"]
        if len(groups) < 2:
            out["warning"] = "This sector has fewer than 2 industry groups with data for this view."
            return out
    else:
        segs = {n: d["segments"][cap]["series"] for n, d in loaded.items() if cap in d["segments"]}
        if len(segs) < 3:
            out["warning"] = f"Load at least 3 sectors first ({len(segs)} loaded): a sector is measured against the others."
            return out
        daily = {n: dict(zip(s["daily"]["dates"], s["daily"]["values"])) for n, s in segs.items()}
        dts, vals = equal_weight_index(daily)
        r = resample(dts, vals, tf)
        bd, bv = r["dates"], r["values"]
        groups = {n: s[tf] for n, s in segs.items()}
        out["benchmark"] = f"equal-weight of the {len(segs)} sectors loaded"
    items = [it for n, s in groups.items() if (it := _item(n, s, bd, bv, p))]
    if not items:
        out["warning"] = f"Not enough {tf} history for the rotation window ({p['win'] + p['lag']} periods needed)."
        return out
    items.sort(key=lambda x: (not x["early"], -x["mom"]))
    out["items"] = items
    out["counts"] = {q: sum(1 for x in items if x["quadrant"] == q) for q in ("leading", "improving", "weakening", "lagging")}
    return out


# ---------------------------------------------------------------------------------------------------------------
# Stocks to review: a mechanical shortlist from the sectors already loaded. NOT a recommendation or advice.
# A stock needs a positive 1-month return, not be below its 50-day average, and either a LONG setup (trend or
# support/resistance rules) or a 1-month return ahead of its sector, inside a sector with a score of at least 40
# or one that is an early mover in the rotation view. Pick score, out of 100:
#   sector  : 0.3 x the sector score (0-100), plus 10 if the sector is an early mover        (up to 40)
#   vs sector: 15 if 1M return beats the sector, 5 if 1W is positive, 10 if above 50-day      (up to 30)
#   setups  : 15 for a LONG trend/momentum setup, 15 for a LONG support/resistance setup      (up to 30)
# At most `per_sector` stocks per sector, so one sector can't fill the list.
# ---------------------------------------------------------------------------------------------------------------
_QUAD = {"leading": "Leading", "improving": "Improving", "weakening": "Weakening", "lagging": "Lagging"}


def picks(loaded: dict[str, dict[str, Any]], cap: str = "all", tf: str = "weekly", limit: int = 15, per_sector: int = 4) -> dict[str, Any]:
    rows = {r["name"]: r for r in rank(loaded, cap)}
    rot = rotation(loaded, tf, cap, "sectors")
    quad = {i["name"]: i for i in rot["items"]}
    cands: list[dict[str, Any]] = []
    for name, d in loaded.items():
        r = rows.get(name)
        if not r:
            continue
        q = quad.get(name)
        early = bool(q and q["early"])
        if r["score"] < 40 and not early:
            continue
        sec_m1 = d["segments"].get(cap, d["segments"]["all"])["metrics"].get("m1")
        for s in d["stocks"]:
            if cap != "all" and (cap == "large") != (s["cap"] == "large"):
                continue
            if s["m1"] is None or s["m1"] <= 0 or s["above_sma50"] is False:
                continue
            rel = None if sec_m1 is None else round(s["m1"] - sec_m1, 2)
            if not (s["trend_long"] or s["sr_long"] or (rel is not None and rel > 0)):
                continue
            score = round(0.3 * r["score"] + (10 if early else 0) + (15 if rel is not None and rel > 0 else 0)
                          + (5 if (s["w1"] or 0) > 0 else 0) + (10 if s["above_sma50"] else 0)
                          + (15 if s["trend_long"] else 0) + (15 if s["sr_long"] else 0))
            why = [f"Sector {r['status']} (score {r['score']})" + (f", rotation: {_QUAD[q['quadrant']]}" if q else "") + (" (early mover)" if early else "")]
            if rel is not None:
                why.append(f"1M {s['m1']:+.1f}% vs sector {sec_m1:+.1f}%" + (" (ahead)" if rel > 0 else ""))
            if s["above_sma50"]:
                why.append("Above its 50-day average")
            if s["trend_long"]:
                why.append("Trend and momentum setup is LONG")
            if s["sr_long"]:
                why.append("Support/resistance setup is LONG" + (f" ({s['sr_setup'].replace('_', ' ')})" if s.get("sr_setup") else ""))
            warn = []
            if s["m1"] > 25:
                warn.append("Up more than 25% in a month: may be extended")
            if s["cap"] != "large":
                warn.append("Mid or small cap: check liquidity and costs")
            if not s.get("ltp_source"):
                warn.append("End-of-day price only, no live price")
            cands.append({"symbol": s["symbol"], "sector": name, "industry": s.get("industry", ""), "cap": s["cap"], "score": score,
                          "close": s["close"], "d1": s["d1"], "w1": s["w1"], "m1": s["m1"], "vs_sector": rel,
                          "trend_long": s["trend_long"], "sr_long": s["sr_long"], "sr_setup": s.get("sr_setup"),
                          "sector_score": r["score"], "sector_status": r["status"], "quadrant": q["quadrant"] if q else None,
                          "early": early, "reasons": why, "cautions": warn})
    cands.sort(key=lambda p: (-p["score"], -(p["m1"] or 0)))
    counts: dict[str, int] = {}
    final: list[dict[str, Any]] = []
    for p in cands:
        if counts.get(p["sector"], 0) >= per_sector:
            continue
        counts[p["sector"]] = counts.get(p["sector"], 0) + 1
        final.append(p)
        if len(final) >= limit:
            break
    return {"tf": tf, "cap": cap, "picks": final, "sectors_loaded": len(loaded), "candidates": len(cands), "rotation_warning": rot["warning"]}
