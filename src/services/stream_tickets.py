"""
Curatarr - Stream Ticket Service

Provides short-lived, one-time tickets for SSE streams.
This allows us to avoid passing JWT tokens in URL query parameters.
"""

import uuid
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# In-memory ticket store: {ticket_id: {"user_id": int, "expires": float}}
_tickets: dict[str, dict] = {}

TICKET_TTL = 60  # seconds


def create_ticket(user_id: int) -> str:
    """Create a short-lived ticket for a user."""
    # Clean up expired tickets
    _cleanup_expired()

    ticket_id = str(uuid.uuid4())
    _tickets[ticket_id] = {
        "user_id": user_id,
        "expires": time.time() + TICKET_TTL
    }
    logger.debug("Created stream ticket for user %d: %s", user_id, ticket_id)
    return ticket_id


def redeem_ticket(ticket_id: str) -> Optional[int]:
    """Validate and redeem a ticket, returning the user_id if valid."""
    _cleanup_expired()

    ticket = _tickets.pop(ticket_id, None)
    if not ticket:
        return None

    if time.time() > ticket["expires"]:
        return None

    return ticket["user_id"]


def _cleanup_expired():
    """Remove expired tickets from the store."""
    now = time.time()
    expired = [tid for tid, data in _tickets.items() if now > data["expires"]]
    for tid in expired:
        _tickets.pop(tid, None)
