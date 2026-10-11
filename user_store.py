"""Per-user settings storage (funding rules and preferences). Plain synchronous functions:
call them from async code with asyncio.to_thread, or from ordinary `def` routes."""
from typing import Any, Optional

import db


def _row(s, user_id: int) -> db.UserSettings:
    row = s.get(db.UserSettings, user_id)
    if row is None:
        row = db.UserSettings(user_id=user_id)
        s.add(row)
    return row


def get_funding_rules(user_id: int) -> Optional[dict[str, Any]]:
    with db.SessionLocal() as s:
        row = s.get(db.UserSettings, user_id)
        return dict(row.funding_rules) if row is not None and row.funding_rules else None


def set_funding_rules(user_id: int, rules: dict[str, Any]) -> None:
    with db.SessionLocal() as s:
        _row(s, user_id).funding_rules = rules
        s.commit()


def clear_funding_rules(user_id: int) -> None:
    with db.SessionLocal() as s:
        row = s.get(db.UserSettings, user_id)
        if row is not None:
            row.funding_rules = None
            s.commit()


def get_prefs(user_id: int) -> dict[str, Any]:
    with db.SessionLocal() as s:
        row = s.get(db.UserSettings, user_id)
        return dict(row.prefs) if row is not None and row.prefs else {}


def set_prefs(user_id: int, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge `updates` into the user's preferences and return the result."""
    with db.SessionLocal() as s:
        row = _row(s, user_id)
        row.prefs = {**(row.prefs or {}), **updates}
        s.commit()
        return dict(row.prefs)
