"""
A self-improving layer for the suggestions, built to be careful rather than clever.

What it does
  1. LOG       Every LONG setup a scan finds (not only the five shown) is recorded with the conditions it was found in:
               rule set and setup type, rule score, reward:risk, how wide the stop is, RSI, company size tier, liquidity rank
               and the market regime (what share of the scanned stocks were above their 50-day average that day).
  2. CHECK     Later, each logged setup is checked against real prices with the same forward test the Evaluation tab uses:
               did it reach its target, hit its stop, or run out of time? That gives a net return per setup.
               A one-time backfill replays the rules over the last months of history so learning has something to start from.
  3. LEARN     Outcomes are grouped by those conditions. A group's average return is shrunk toward the overall average
               (so a group with 3 trades says very little), and a setup's "learned edge" is the overall average plus the
               average of its groups' lifts. The suggestions are re-ranked by that edge, and setups whose evidence is clearly
               negative are left out.
  4. PROVE     A new calibration is only adopted if it passes TWO walk-forward tests (trained on older setups, then used to
               rank newer, unseen ones, in two separate later periods): each time the top third by learned edge must beat the
               average by a margin, beat the bottom third, and a shuffle test must say that is unlikely to be luck. Passing
               both makes a lucky run far less likely. Otherwise the plain rules keep ranking. Every calibration is versioned,
               so what was in force when is always known.
  5. MEASURE   Live setups also store where the plain rules and the learned model each ranked them, so the Evaluation tab
               can show, on real outcomes, whether the learned top picks do better than the plain top picks.

What it does NOT do: change the rules' entries, stops or targets, or learn from tiny samples. It cannot promise better
results: markets change, and a model that worked can stop working. Information only, not investment advice.
"""
import datetime as dt
import random
from typing import Any, Optional

import backtest
import sr_model
import technical

K_SHRINK = 15              # pseudo-observations pulling a group toward the overall average
MIN_TOTAL, MIN_VALID = 200, 40    # resolved setups needed before testing at all, and the least each unseen test set may hold
MIN_LIFT = 0.25            # % by which the top third must beat the average on unseen setups
MAX_P = 0.10               # largest shuffle-test p-value accepted, in EACH of two separate tests
AVOID_EDGE, AVOID_MIN_N = -0.5, 25      # leave out a setup whose learned edge is below this, if backed by at least this many past setups
COST_PCT = 0.2
FEATURES = ("setup", "score", "rr", "risk", "rsi", "cap", "liq", "regime")


# ---------------------------------------------------------------- conditions
def _bucket(v: Optional[float], cuts: list[float], labels: list[str]) -> str:
    if v is None:
        return "unknown"
    for c, lab in zip(cuts, labels):
        if v < c:
            return lab
    return labels[-1]


def features_for(c: dict[str, Any], regime: Optional[float] = None, tier: Optional[str] = None) -> dict[str, str]:
    """The conditions a setup was found in, as coarse buckets. c needs entry, stop_loss, target, score; rsi, setup and rank are optional."""
    entry, stop, target = c["entry"], c["stop_loss"], c["target"]
    rr = (target - entry) / (entry - stop) if entry > stop else None
    risk = (entry - stop) / entry * 100 if entry else None
    rank = c.get("rank")
    return {"setup": c.get("setup") or "trend_momentum", "score": str(c["score"]),
            "rr": _bucket(rr, [2.0, 2.5], ["<2", "2-2.5", ">2.5"]), "risk": _bucket(risk, [3.0, 5.0], ["<3%", "3-5%", ">5%"]),
            "rsi": _bucket(c.get("rsi"), [40, 50, 60, 70], ["<40", "40-50", "50-60", "60-70", ">70"]),
            "cap": tier or "unknown",
            "liq": "unknown" if rank is None else "top30" if rank < 30 else "top100" if rank < 100 else "rest",
            "regime": "unknown" if regime is None else "weak" if regime < 40 else "mixed" if regime <= 60 else "strong"}


# ---------------------------------------------------------------- calibration
def calibrate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Group outcomes by condition; shrink each group's average return and win rate toward the overall figures."""
    n = len(rows)
    rets = [r["return_pct"] for r in rows]
    mu0 = sum(rets) / n if n else 0.0
    win0 = sum(1 for x in rets if x > 0) / n if n else 0.0
    buckets: dict[str, dict[str, dict[str, float]]] = {}
    for r in rows:
        for f, b in r["features"].items():
            if f not in FEATURES:
                continue
            s = buckets.setdefault(f, {}).setdefault(b, {"n": 0, "wins": 0, "sum": 0.0})
            s["n"] += 1
            s["sum"] += r["return_pct"]
            s["wins"] += 1 if r["return_pct"] > 0 else 0
    for bs in buckets.values():
        for s in bs.values():
            s["mean"] = s["sum"] / s["n"]
            s["shrunk"] = (s["sum"] + K_SHRINK * mu0) / (s["n"] + K_SHRINK)
            s["win"] = (s["wins"] + K_SHRINK * win0) / (s["n"] + K_SHRINK)
    return {"n": n, "mu0": mu0, "win0": win0, "buckets": buckets}


def score(model: Optional[dict[str, Any]], feats: dict[str, str]) -> Optional[dict[str, Any]]:
    """Learned edge (expected net return %), win probability and the evidence behind it (the smallest group count used)."""
    if not model or not model.get("n"):
        return None
    lifts, wins, counts = [], [], []
    for f in FEATURES:
        b = feats.get(f)
        s = model["buckets"].get(f, {}).get(b) if b not in (None, "unknown") else None
        if s:
            lifts.append(s["shrunk"] - model["mu0"])
            wins.append(s["win"] - model["win0"])
            counts.append(s["n"])
    if not lifts:
        return {"edge": round(model["mu0"], 3), "win_prob": round(model["win0"], 3), "n": 0}
    return {"edge": round(model["mu0"] + sum(lifts) / len(lifts), 3), "win_prob": round(model["win0"] + sum(wins) / len(wins), 3), "n": min(counts)}


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _fold(rows: list[dict[str, Any]], a: int, b: int) -> dict[str, Any]:
    """Train on rows[:a] (older), rank rows[a:b] (newer, unseen) and see whether the learned order really picks the better setups."""
    tr, va = rows[:a], rows[a:b]
    m = calibrate(tr)
    pairs = sorted(((score(m, r["features"])["edge"], r["return_pct"]) for r in va), key=lambda p: p[0])
    third = max(len(pairs) // 3, 1)
    rets = [p[1] for p in pairs]
    top, bottom = [p[1] for p in pairs[-third:]], [p[1] for p in pairs[:third]]
    base, lift = _mean(rets), _mean(top) - _mean(rets)
    rnd, hits, B = random.Random(7), 0, 300            # shuffle test: how often does a random ordering do at least this well?
    for _ in range(B):
        shuffled = rets[:]
        rnd.shuffle(shuffled)
        hits += 1 if _mean(shuffled[-third:]) - base >= lift else 0
    p_value = (hits + 1) / (B + 1)
    ok = len(va) >= MIN_VALID and lift >= MIN_LIFT and _mean(top) > _mean(bottom) and p_value <= MAX_P
    return {"n_train": len(tr), "n_valid": len(va), "baseline_mean": round(base, 3), "top_mean": round(_mean(top), 3), "bottom_mean": round(_mean(bottom), 3),
            "lift": round(lift, 3), "p_value": round(p_value, 3), "top_n": third, "valid_from": str(va[0]["as_of"]) if va else None,
            "valid_to": str(va[-1]["as_of"]) if va else None, "pass": ok, "_rets": rets, "_top": top, "_bottom": bottom}


def train(rows: list[dict[str, Any]], now: Optional[str] = None) -> dict[str, Any]:
    """Fit on resolved setups and decide, on two separate stretches of newer unseen setups, whether the calibration is good enough to use."""
    rows = sorted((r for r in rows if r.get("return_pct") is not None and r.get("features")), key=lambda r: str(r["as_of"]))
    n = len(rows)
    out: dict[str, Any] = {"created": now or dt.datetime.now().isoformat(timespec="seconds"), "n": n, "adopted": False, "reason": None,
                           "validation": None, "model": calibrate(rows)}
    if n < MIN_TOTAL:
        out["reason"] = f"Needs at least {MIN_TOTAL} resolved setups before it can be tested; has {n}. The plain rules keep ranking."
        return out
    folds = [_fold(rows, int(n * 0.55), int(n * 0.80)), _fold(rows, int(n * 0.80), n)]
    rets, top, bottom = (sum((f[k] for f in folds), []) for k in ("_rets", "_top", "_bottom"))
    base, lift = _mean(rets), _mean(top) - _mean(rets)
    for f in folds:
        for k in ("_rets", "_top", "_bottom"):
            f.pop(k)
    out["validation"] = {"folds": folds, "n_valid": sum(f["n_valid"] for f in folds), "baseline_mean": round(base, 3), "top_mean": round(_mean(top), 3),
                         "bottom_mean": round(_mean(bottom), 3), "lift": round(lift, 3), "p_value": max(f["p_value"] for f in folds)}
    if all(f["pass"] for f in folds):
        out["adopted"] = True
        out["reason"] = (f"Passed both walk-forward tests: on {out['validation']['n_valid']} newer, unseen setups the top third by learned edge averaged "
                         f"{_mean(top):+.2f}% against {base:+.2f}% for all (worst shuffle-test p = {out['validation']['p_value']:.2f}), so it is used to re-rank suggestions.")
    else:
        failed = [i + 1 for i, f in enumerate(folds) if not f["pass"]]
        out["reason"] = (f"Not adopted: it must pass both tests on unseen setups (top third at least +{MIN_LIFT:.2f}% better than average, better than the bottom third, "
                         f"shuffle-test p <= {MAX_P}); test {' and '.join(map(str, failed))} did not (top third {_mean(top):+.2f}% against {base:+.2f}% for all). The plain rules keep ranking.")
    return out


# ---------------------------------------------------------------- what the page and the ranking read
def bucket_table(model: Optional[dict[str, Any]], min_n: int = 15) -> list[dict[str, Any]]:
    """Every condition group with enough setups, strongest first."""
    if not model:
        return []
    rows = [{"feature": f, "bucket": b, "n": int(s["n"]), "mean": round(s["mean"], 2), "win_pct": round(100 * s["wins"] / s["n"]), "lift": round(s["shrunk"] - model["mu0"], 2)}
            for f, bs in model["buckets"].items() for b, s in bs.items() if s["n"] >= min_n and b != "unknown"]
    return sorted(rows, key=lambda r: -r["lift"])


def rr_of(c: dict[str, Any]) -> float:
    return (c["target"] - c["entry"]) / (c["entry"] - c["stop_loss"]) if c["entry"] > c["stop_loss"] else 0.0


def rows_from_scan(result: dict[str, Any], learned: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Log rows for every LONG in a scan, with the rank the plain rules and the learned model each gave it."""
    longs = result.get("longs", [])
    base_order = sorted(longs, key=lambda l: (-int(l["score"][0]), -rr_of(l), l["symbol"]))
    base_rank = {l["symbol"]: i + 1 for i, l in enumerate(base_order)}
    learned_rank = dict(base_rank)
    version = None
    if learned and learned.get("model"):
        edges = {l["symbol"]: (score(learned["model"], l["features"]) or {"edge": 0.0})["edge"] for l in longs}
        order = sorted(longs, key=lambda l: (-edges[l["symbol"]], -int(l["score"][0]), l["symbol"]))
        learned_rank = {l["symbol"]: i + 1 for i, l in enumerate(order)}
        version = learned.get("version")
    return [{"symbol": l["symbol"], "as_of": l["as_of"], "model": l["model"], "setup": l["setup"], "score": l["score"], "entry": l["entry"],
             "stop_loss": l["stop_loss"], "target": l["target"], "features": l["features"], "source": "scan", "status": "open",
             "rank_baseline": base_rank[l["symbol"]], "rank_learned": learned_rank[l["symbol"]], "model_version": version} for l in longs]


def ab_compare(rows: list[dict[str, Any]], top: int = 5) -> dict[str, Any]:
    """On resolved live setups, per scan day: the plain rules' top picks against the learned model's top picks."""
    days: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r.get("source") == "scan" and r.get("return_pct") is not None and r.get("model_version") is not None and r.get("rank_learned") is not None:
            days.setdefault(str(r["as_of"]), []).append(r)
    b_all, l_all, n_days = [], [], 0
    for d, rs in days.items():
        if len(rs) < top + 2:
            continue
        b = [r["return_pct"] for r in rs if r["rank_baseline"] <= top]
        l = [r["return_pct"] for r in rs if r["rank_learned"] <= top]
        if b and l:
            b_all += b; l_all += l; n_days += 1
    if not n_days:
        return {"days": 0, "note": "Not enough resolved live setups from days the learned model was in use yet."}
    return {"days": n_days, "plain_avg": round(_mean(b_all), 2), "learned_avg": round(_mean(l_all), 2), "diff": round(_mean(l_all) - _mean(b_all), 2),
            "plain_n": len(b_all), "learned_n": len(l_all), "plain_win": round(100 * sum(1 for x in b_all if x > 0) / len(b_all)), "learned_win": round(100 * sum(1 for x in l_all if x > 0) / len(l_all))}


# ---------------------------------------------------------------- outcomes
def resolve(rows: list[dict[str, Any]], records_by_symbol: dict[str, list[dict[str, Any]]], max_hold: int = 20, cost: float = COST_PCT) -> list[dict[str, Any]]:
    """Check open setups against later prices; returns updates for the ones that reached target, stop or the time limit."""
    updates = []
    for r in rows:
        recs = records_by_symbol.get(r["symbol"])
        if not recs:
            continue
        ev = backtest.evaluate_forward(recs, str(r["as_of"]), r["entry"], r["stop_loss"], r["target"], max_hold, cost)
        if ev.get("status") in ("target", "stopped", "expired"):
            updates.append({"id": r["id"], "status": ev["status"], "return_pct": ev["return_pct"], "bars": ev["bars"], "exit_date": ev["exit_date"]})
    return updates


def backfill_rows(sym: str, recs: list[dict[str, Any]], tier: Optional[str] = None, rank: Optional[int] = None,
                  max_hold: int = 20, cost: float = COST_PCT) -> list[dict[str, Any]]:
    """Replay both rule sets over a stock's history: resolved past setups to learn from (regime is unknown for these)."""
    p = backtest.prepare(recs)
    if len(p["c"]) < 70:
        return []
    idx = {d: i for i, d in enumerate(p["dates"])}
    rsi = technical.rsi(p["c"], 14)
    out: list[dict[str, Any]] = []
    for kind in ("trend", "sr"):
        setups: dict[str, str] = {}
        fn = None
        if kind == "sr":
            def fn(pp, i, _s=setups):
                sig = sr_model.signal_from_prepared(pp, i)
                if sig.get("signal") == "LONG":
                    _s[pp["dates"][i]] = sig.get("setup")
                return sig
        try:
            res = backtest.run(recs, max_hold, cost, signal_fn=fn)
        except ValueError:
            continue
        for t in res["trades"]:
            if t["reason"] == "open":
                continue
            i = idx.get(t["entry_date"])
            setup = "trend_momentum" if kind == "trend" else setups.get(t["entry_date"], "support_bounce")
            cand = {"entry": t["entry"], "stop_loss": t["stop"], "target": t["target"], "score": t["score"], "setup": setup,
                    "rsi": rsi[i] if i is not None else None, "rank": rank}
            out.append({"symbol": sym, "as_of": t["entry_date"], "model": kind, "setup": setup, "score": t["score"], "entry": t["entry"], "stop_loss": t["stop"],
                        "target": t["target"], "features": features_for(cand, None, tier), "source": "backfill",
                        "status": {"target": "target", "stop": "stopped", "time": "expired"}[t["reason"]], "return_pct": t["ret_pct"],
                        "bars": t["bars"], "exit_date": t["exit_date"]})
    return out


# ---------------------------------------------------------------- storage (the database; imported only when used)
def _d(r: Any) -> dict[str, Any]:
    return {"id": r.id, "symbol": r.symbol, "as_of": r.as_of, "model": r.model, "setup": r.setup, "score": r.score, "entry": r.entry, "stop_loss": r.stop_loss,
            "target": r.target, "features": r.features or {}, "source": r.source, "status": r.status, "return_pct": r.return_pct, "bars": r.bars,
            "exit_date": r.exit_date, "rank_baseline": r.rank_baseline, "rank_learned": r.rank_learned, "model_version": r.model_version}


def log_rows(rows: list[dict[str, Any]]) -> int:
    """Insert setups not already logged (same stock, date, rule set, setup and source). Returns how many were new."""
    if not rows:
        return 0
    import db
    L = db.SuggestionLog
    with db.SessionLocal() as s:
        have = {tuple(x) for x in s.query(L.symbol, L.as_of, L.model, L.setup, L.source).filter(L.as_of.in_({str(r["as_of"]) for r in rows}))}
        added = 0
        for r in rows:
            key = (r["symbol"], str(r["as_of"]), r["model"], r["setup"], r["source"])
            if key in have:
                continue
            s.add(L(symbol=r["symbol"], as_of=str(r["as_of"]), model=r["model"], setup=r["setup"], score=r["score"], entry=r["entry"], stop_loss=r["stop_loss"],
                    target=r["target"], features=r["features"], source=r["source"], status=r.get("status", "open"), return_pct=r.get("return_pct"),
                    bars=r.get("bars"), exit_date=r.get("exit_date"), rank_baseline=r.get("rank_baseline"), rank_learned=r.get("rank_learned"),
                    model_version=r.get("model_version")))
            have.add(key)
            added += 1
        s.commit()
        return added


def fetch_rows(open_only: bool = False, resolved_only: bool = False) -> list[dict[str, Any]]:
    import db
    L = db.SuggestionLog
    with db.SessionLocal() as s:
        q = s.query(L)
        if open_only:
            q = q.filter(L.status == "open")
        if resolved_only:
            q = q.filter(L.status != "open")
        return [_d(r) for r in q]


def update_outcomes(updates: list[dict[str, Any]]) -> None:
    if not updates:
        return
    import db
    with db.SessionLocal() as s:
        for u in updates:
            r = s.get(db.SuggestionLog, u["id"])
            if r is not None:
                r.status, r.return_pct, r.bars, r.exit_date = u["status"], u["return_pct"], u["bars"], u["exit_date"]
        s.commit()


def save_model(payload: dict[str, Any]) -> int:
    """Store a trained calibration as the next version. Returns its version number."""
    import db
    with db.SessionLocal() as s:
        last = s.query(db.LearnedModel).order_by(db.LearnedModel.version.desc()).first()
        version = (last.version if last else 0) + 1
        s.add(db.LearnedModel(version=version, adopted=bool(payload.get("adopted")), payload={**payload, "version": version}))
        s.commit()
        return version


def latest_models() -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """(the newest trained calibration, the newest one that was adopted), as payloads with their version numbers."""
    import db
    M = db.LearnedModel
    with db.SessionLocal() as s:
        newest = s.query(M).order_by(M.version.desc()).first()
        adopted = s.query(M).filter(M.adopted.is_(True)).order_by(M.version.desc()).first()
        return (newest.payload if newest else None, adopted.payload if adopted else None)


def counts() -> dict[str, int]:
    import db
    from sqlalchemy import func
    L = db.SuggestionLog
    with db.SessionLocal() as s:
        out = {"logged": 0, "open": 0, "resolved": 0, "live_logged": 0, "live_resolved": 0, "backfilled": 0}
        for status, source, n in s.query(L.status, L.source, func.count(L.id)).group_by(L.status, L.source):
            out["logged"] += n
            out["open" if status == "open" else "resolved"] += n
            if source == "scan":
                out["live_logged"] += n
                out["live_resolved"] += 0 if status == "open" else n
            else:
                out["backfilled"] += n
        return out
