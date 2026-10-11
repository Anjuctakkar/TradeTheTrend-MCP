"""Per-user chat quota and token log, stored in the chat_calls table so the limits hold across
instances and restarts. Synchronous functions: call them with asyncio.to_thread from async code.

A day runs from midnight to midnight India time. The check-then-record step is not locked, so two
requests arriving in the same instant can overshoot a limit by one; that is acceptable for a cost guard."""
import datetime as dt
import random
from typing import Any

from sqlalchemy import func

import db

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
KEEP_DAYS = 30


class LimitReached(Exception):
    """The message is safe to show to the user."""


def _day_start() -> dt.datetime:
    return dt.datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc)


def start_call(user_id: int, per_10min: int, per_day: int, global_per_day: int) -> int:
    """Check this user's limits and the all-users limit; record the call and return its id."""
    now = dt.datetime.now(dt.timezone.utc)
    day = _day_start()
    with db.SessionLocal() as s:
        def count(*conds) -> int:
            return s.query(func.count(db.ChatCall.id)).filter(*conds).scalar() or 0

        if global_per_day and count(db.ChatCall.created_at >= day) >= global_per_day:
            raise LimitReached("The assistant has reached its daily limit. Please try again tomorrow.")
        if per_day and count(db.ChatCall.user_id == user_id, db.ChatCall.created_at >= day) >= per_day:
            raise LimitReached(f"You've used today's {per_day} questions. The count resets at midnight India time.")
        if per_10min and count(db.ChatCall.user_id == user_id, db.ChatCall.created_at >= now - dt.timedelta(minutes=10)) >= per_10min:
            raise LimitReached("Too many questions in a short time. Please wait a few minutes.")
        row = db.ChatCall(user_id=user_id)
        s.add(row)
        if random.random() < 0.01:       # occasional housekeeping
            s.query(db.ChatCall).filter(db.ChatCall.created_at < now - dt.timedelta(days=KEEP_DAYS)).delete()
        s.commit()
        return row.id


def finish_call(call_id: int, tokens: dict[str, int]) -> None:
    with db.SessionLocal() as s:
        row = s.get(db.ChatCall, call_id)
        if row is not None:
            row.input_tokens, row.output_tokens = tokens.get("input", 0), tokens.get("output", 0)
            s.commit()


def used_today(user_id: int) -> int:
    with db.SessionLocal() as s:
        return s.query(func.count(db.ChatCall.id)).filter(db.ChatCall.user_id == user_id, db.ChatCall.created_at >= _day_start()).scalar() or 0


def report(days: int = 7) -> list[dict[str, Any]]:
    """Per-user questions and tokens, for the administrator."""
    day, since = _day_start(), _day_start() - dt.timedelta(days=days - 1)
    with db.SessionLocal() as s:
        rows = (s.query(db.User.email, func.count(db.ChatCall.id), func.coalesce(func.sum(db.ChatCall.input_tokens), 0),
                        func.coalesce(func.sum(db.ChatCall.output_tokens), 0))
                .join(db.ChatCall, db.ChatCall.user_id == db.User.id).filter(db.ChatCall.created_at >= since)
                .group_by(db.User.email).all())
        today = dict(s.query(db.User.email, func.count(db.ChatCall.id)).join(db.ChatCall, db.ChatCall.user_id == db.User.id)
                     .filter(db.ChatCall.created_at >= day).group_by(db.User.email).all())
    out = [{"email": e, "questions_today": today.get(e, 0), f"questions_{days}d": int(n), f"input_tokens_{days}d": int(i), f"output_tokens_{days}d": int(o)}
           for e, n, i, o in rows]
    return sorted(out, key=lambda r: -r[f"questions_{days}d"])
