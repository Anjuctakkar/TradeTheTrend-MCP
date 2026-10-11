"""Each user's evaluation list (stocks they ticked, plus stocks the system suggested to them).

Stored in the watchlist_entries table. EVERY function takes the user_id and only ever touches that
user's rows, so one user can never see, change or delete another's entries (a wrong id behaves
exactly like a missing one).
"""
import asyncio
import datetime as dt
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError

import db


def _dict(r: db.WatchlistEntry) -> dict[str, Any]:
    return {"id": r.id, "symbol": r.symbol, "source": r.source, "as_of": r.as_of,
            "entry": r.entry, "stop_loss": r.stop_loss, "target": r.target,
            "score": r.score, "hold_days": r.hold_days,
            "logged_at": r.logged_at.isoformat(timespec="seconds") if r.logged_at else None, "taken": bool(r.taken)}


def _add(user_id: int, items: list[dict[str, Any]], source: str) -> dict[str, Any]:
    for attempt in (1, 2):                      # the 2nd pass only happens if two requests raced
        with db.SessionLocal() as s:
            existing = {(r.symbol, r.as_of, r.source): r.id
                        for r in s.query(db.WatchlistEntry).filter_by(user_id=user_id)}
            added, ids = 0, []
            for p in items:
                key = (p["symbol"], p["as_of"], source)
                if key in existing:
                    ids.append(existing[key])
                    continue
                row = db.WatchlistEntry(
                    id=uuid.uuid4().hex[:12], user_id=user_id, symbol=p["symbol"], source=source, as_of=p["as_of"],
                    entry=p.get("entry"), stop_loss=p.get("stop_loss"), target=p.get("target"),
                    score=p.get("score"), hold_days=p.get("hold_days"), taken=False)
                s.add(row)
                existing[key] = row.id
                ids.append(row.id)
                added += 1
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
                if attempt == 2:
                    raise
                continue
            return {"added": added, "skipped": len(ids) - added, "ids": ids}
    raise RuntimeError("unreachable")


def _list(user_id: int) -> list[dict[str, Any]]:
    with db.SessionLocal() as s:
        return [_dict(r) for r in s.query(db.WatchlistEntry).filter_by(user_id=user_id)]


def _set_taken(user_id: int, entry_id: str, taken: bool) -> bool:
    with db.SessionLocal() as s:
        r = s.query(db.WatchlistEntry).filter_by(user_id=user_id, id=entry_id).one_or_none()
        if r is None:
            return False
        r.taken = taken
        s.commit()
        return True


def _delete(user_id: int, entry_id: str) -> bool:
    with db.SessionLocal() as s:
        r = s.query(db.WatchlistEntry).filter_by(user_id=user_id, id=entry_id).one_or_none()
        if r is None:
            return False
        s.delete(r)
        s.commit()
        return True


async def add_items(items: list[dict[str, Any]], source: str, user_id: int) -> dict[str, Any]:
    """Add stocks to the user's list. source is "user" (ticked by them) or "system" (found by Suggest
    stocks). The same symbol on the same as_of date from the same source is only added once. Levels
    are optional: a stock with no LONG setup is kept as watch-only. Returns the ids of every requested
    item, including ones that were already there."""
    return await asyncio.to_thread(_add, user_id, items, source)


async def list_entries(user_id: int) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_list, user_id)


async def set_taken(entry_id: str, taken: bool, user_id: int) -> bool:
    return await asyncio.to_thread(_set_taken, user_id, entry_id, taken)


async def delete(entry_id: str, user_id: int) -> bool:
    return await asyncio.to_thread(_delete, user_id, entry_id)
