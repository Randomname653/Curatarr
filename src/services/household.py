"""Curatarr — household roles shared by the background paths.

The owner (``User.is_admin``) curates the library; household members only
shape their own taste and, through Pillar III, protect what they hold. A
background job that applies owner-only data (the owner's SoulSync follows)
or does owner-only work (principle capture for the global judge) asks here.
"""

import logging

logger = logging.getLogger(__name__)


def is_admin_user(user_id) -> bool:
    """True when ``user_id`` is the owner. Blocking — call via ``run_read``.

    Fails closed: an unreadable row reads as a member, so owner-only data is
    withheld for one run rather than leaked into a member's profile."""
    if user_id is None:
        return False
    try:
        from src.database.connection import get_db_session
        from src.database.models import User
        with get_db_session() as db:
            row = db.query(User.is_admin).filter(User.id == int(user_id)).first()
        return bool(row and row[0])
    except Exception as e:
        logger.warning("[household] admin lookup failed for user %s: %s", user_id, e)
        return False
