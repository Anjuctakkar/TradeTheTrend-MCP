"""
Shared cache for NSE MCP data.

Market data is the same for every user, so it is fetched once and shared:

  * L1: a small in-process dictionary (fastest).
  * L2: the nse_cache table, so a restart, a second instance, or a deploy keeps the cache warm.
  * Single-flight: if ten users ask for the same thing at once, one request goes to NSE and the rest wait for it.
  * Stale-on-error: if NSE is slow or down and an expired copy exists, serve it (marked "stale": true) instead of
    failing. Live quotes are never served stale.
  * Errors and empty answers are never cached.

How long things live (all adjustable by environment variable)
  End-of-day (EOD) data   NSE publishes the day's Bhavcopy some time after the 15:30 close, so between 16:00 and
                          22:00 India time a copy lives 15 minutes (NSE_EOD_TTL_PUBLISH), otherwise 6 hours
                          (NSE_EOD_TTL_IDLE). A copy never outlives the next 16:00 or 22:00 IST boundary, so
                          yesterday's data cannot hide today's publication.
  Older history chunks    24 hours (explicit past end dates don't change).
  Corporate actions       6 hours. Symbol search: 24 hours.
  Live quotes and movers  30 seconds (NSE_LIVE_TTL): the source itself refreshes about once a minute, so
                          everyone watching the same stocks shares one fetch.

Set NSE_CACHE=0 to switch the cache off.
"""
import asyncio
import copy
import datetime as dt
import hashlib
import json
import os
import random
import time
from collections import Counter
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import func

import db

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ENABLED = os.getenv("NSE_CACHE", "1").lower() not in ("0", "false", "no", "off")
LIVE_TTL = float(os.getenv("NSE_LIVE_TTL", "30"))
EOD_TTL_PUBLISH = float(os.getenv("NSE_EOD_TTL_PUBLISH", "900"))
EOD_TTL_IDLE = float(os.getenv("NSE_EOD_TTL_IDLE", "21600"))
L1_MAX = int(os.getenv("NSE_CACHE_L1_MAX", "3000"))
HOUR = 3600.0


# ------------------------------------------------------------------ time policies
def _next_boundary(now: dt.datetime) -> dt.datetime:
    """The next 16:00 or 22:00 India time after `now` (EOD data may appear around then)."""
    local = now.astimezone(IST)
    for hour in (16, 22):
        cand = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if cand > local:
            return cand
    return (local + dt.timedelta(days=1)).replace(hour=16, minute=0, second=0, microsecond=0)


def _eod_ttl(args: dict, now: dt.datetime) -> float:
    local = now.astimezone(IST)
    minutes = local.hour * 60 + local.minute
    ttl = EOD_TTL_PUBLISH if (local.weekday() < 5 and 16 * 60 <= minutes < 22 * 60) else EOD_TTL_IDLE
    return min(ttl, (_next_boundary(now) - now).total_seconds())


def _history_ttl(args: dict, now: dt.datetime) -> float:
    end = str(args.get("endDate", "today"))
    if end != "today":
        try:
            if dt.date.fromisoformat(end[:10]) < now.astimezone(IST).date() - dt.timedelta(days=2):
                return 24 * HOUR
        except ValueError:
            pass
    return _eod_ttl(args, now)


class Policy:
    def __init__(self, ttl: Callable[[dict, dt.datetime], float], stale_for: float):
        self.ttl, self.stale_for = ttl, stale_for      # stale_for: how long past expiry an answer may be served if NSE fails


def _live(args: dict, now: dt.datetime) -> float:
    return LIVE_TTL


POLICIES: dict[str, Policy] = {
    # Bhavcopy server (end of day)
    "get_stock_history": Policy(_history_ttl, 24 * HOUR),
    "get_ltp_by_date": Policy(_eod_ttl, 24 * HOUR),
    "moving_average": Policy(_eod_ttl, 24 * HOUR),
    "get_top_by_volume": Policy(_eod_ttl, 24 * HOUR),
    "get_bulk_quote": Policy(_eod_ttl, 24 * HOUR),
    "get_corporate_actions": Policy(lambda a, n: 6 * HOUR, 3 * 24 * HOUR),
    "search_symbols": Policy(lambda a, n: 24 * HOUR, 7 * 24 * HOUR),
    # CM server (live): short life, never stale
    "cm_get_stock_quote": Policy(_live, 0),
    "nse_get_market_movers": Policy(_live, 0),
    "cm_get_data_status": Policy(_live, 0),
    "cm_get_allstocks_status": Policy(_live, 0),
}


# ------------------------------------------------------------------ helpers
def _aware(d: dt.datetime) -> dt.datetime:
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)      # SQLite returns naive UTC


def make_key(tool: str, url: str, args: dict) -> str:
    salt = ""
    if any(v == "today" for v in args.values()):                     # "today" means a different day each day
        salt = dt.datetime.now(IST).date().isoformat()
    raw = json.dumps([url, tool, args, salt], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def cacheable(result: Any) -> bool:
    """Only successful, non-empty answers are kept."""
    if not isinstance(result, dict) or result.get("is_error"):
        return False
    content = result.get("content")
    return content not in (None, "", [], {})


# ------------------------------------------------------------------ the cache
class NseCache:
    def __init__(self) -> None:
        self._l1: dict[str, tuple[float, float, str]] = {}          # key -> (fresh_until, stale_until, json)
        self._inflight: dict[str, asyncio.Future] = {}
        self.stats: Counter = Counter()
        self.by_tool: dict[str, Counter] = {}

    def _count(self, tool: str, what: str) -> None:
        self.stats[what] += 1
        self.by_tool.setdefault(tool, Counter())[what] += 1

    # ---- storage
    def _l1_get(self, key: str) -> Optional[tuple[float, float, str]]:
        return self._l1.get(key)

    def _l1_put(self, key: str, fresh_until: float, stale_until: float, value: str) -> None:
        if len(self._l1) >= L1_MAX:
            self._l1.pop(next(iter(self._l1)))                       # oldest entry
        self._l1[key] = (fresh_until, stale_until, value)

    @staticmethod
    def _l2_get(key: str) -> Optional[tuple[float, float, str]]:
        with db.SessionLocal() as s:
            row = s.get(db.NseCacheEntry, key)
            if row is None:
                return None
            return _aware(row.expires_at).timestamp(), _aware(row.stale_until).timestamp(), row.value

    @staticmethod
    def _l2_put(key: str, tool: str, fresh_until: float, stale_until: float, value: str) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        fresh = dt.datetime.fromtimestamp(fresh_until, dt.timezone.utc)
        stale = dt.datetime.fromtimestamp(stale_until, dt.timezone.utc)
        with db.SessionLocal() as s:
            row = s.get(db.NseCacheEntry, key)
            if row is None:
                row = db.NseCacheEntry(key=key, tool=tool)
                s.add(row)
            row.value, row.created_at, row.expires_at, row.stale_until = value, now, fresh, stale
            if random.random() < 0.02:                               # occasional housekeeping
                s.query(db.NseCacheEntry).filter(db.NseCacheEntry.stale_until < now).delete()
            s.commit()

    async def _lookup(self, key: str) -> Optional[tuple[float, float, str]]:
        hit = self._l1_get(key)
        if hit is not None:
            return hit
        try:
            hit = await asyncio.to_thread(self._l2_get, key)
        except Exception as exc:  # noqa: BLE001 - a broken cache table must never break the app
            print(f"[cache] read failed: {type(exc).__name__}: {exc}", flush=True)
            return None
        if hit is not None:
            self._l1_put(key, *hit)
        return hit

    async def _store(self, key: str, tool: str, policy: Policy, args: dict, result: dict) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        fresh_until = time.time() + policy.ttl(args, now)
        stale_until = fresh_until + policy.stale_for
        value = json.dumps(result, default=str)
        self._l1_put(key, fresh_until, stale_until, value)
        try:
            await asyncio.to_thread(self._l2_put, key, tool, fresh_until, stale_until, value)
        except Exception as exc:  # noqa: BLE001
            print(f"[cache] write failed: {type(exc).__name__}: {exc}", flush=True)

    # ---- main entry point
    async def get_or_fetch(self, tool: str, url: str, args: dict, fetch: Callable[[], Awaitable[dict]]) -> dict:
        policy = POLICIES.get(tool)
        if not ENABLED or policy is None:
            return await fetch()
        key = make_key(tool, url, args)

        hit = await self._lookup(key)
        if hit is not None and hit[0] > time.time():
            self._count(tool, "hits")
            return json.loads(hit[2])

        loop = asyncio.get_running_loop()
        pending = self._inflight.get(key)
        if pending is not None and pending.get_loop() is loop:        # someone is already fetching this
            self._count(tool, "coalesced")
            return copy.deepcopy(await asyncio.shield(pending))

        fut: asyncio.Future = loop.create_future()
        self._inflight[key] = fut
        try:
            self._count(tool, "misses")
            try:
                result = await fetch()
            except Exception:
                if hit is not None and hit[1] > time.time():          # NSE failed; an expired copy is still allowed
                    self._count(tool, "stale_served")
                    out = json.loads(hit[2])
                    out["stale"] = True
                    fut.set_result(out)
                    return copy.deepcopy(out)
                self._count(tool, "errors")
                raise
            if cacheable(result):
                await self._store(key, tool, policy, args, result)
            fut.set_result(result)
            return copy.deepcopy(result)
        except BaseException as exc:
            if not fut.done():
                if isinstance(exc, asyncio.CancelledError):
                    fut.cancel()
                else:
                    fut.set_exception(exc)
                    fut.exception()                                    # mark retrieved: no "never retrieved" warning
            raise
        finally:
            self._inflight.pop(key, None)

    # ---- admin
    def snapshot(self) -> dict[str, Any]:
        with db.SessionLocal() as s:
            rows = s.query(db.NseCacheEntry.tool, func.count(db.NseCacheEntry.key)).group_by(db.NseCacheEntry.tool).all()
        hits, misses = self.stats["hits"], self.stats["misses"]
        return {"enabled": ENABLED, "since_start": dict(self.stats),
                "hit_rate_pct": round(100 * (hits + self.stats["coalesced"]) / max(1, hits + self.stats["coalesced"] + misses), 1),
                "by_tool": {t: dict(c) for t, c in sorted(self.by_tool.items())},
                "entries_in_db": {t: n for t, n in rows}, "entries_in_memory": len(self._l1),
                "ttl": {"live_s": LIVE_TTL, "eod_publish_window_s": EOD_TTL_PUBLISH, "eod_idle_s": EOD_TTL_IDLE}}

    def clear(self, tool: Optional[str] = None) -> int:
        """Drop cached entries (all, or one tool's). Returns the number removed from the database."""
        self._l1.clear()                                               # memory refills from the database as needed
        with db.SessionLocal() as s:
            q = s.query(db.NseCacheEntry)
            if tool:
                q = q.filter(db.NseCacheEntry.tool == tool)
            n = q.delete()
            s.commit()
        return n


CACHE = NseCache()
