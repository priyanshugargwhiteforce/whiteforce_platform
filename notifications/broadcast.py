"""
Helper to push a message to every Socket.IO connection subscribed to a
given tag — callable from normal (sync) Django views, signals, management
commands, anywhere.

USAGE
─────
    from notifications.broadcast import broadcast_to_tag

    broadcast_to_tag("job_alerts", {
        "notification_id": 42,
        "domain": "jobs",
        "title": "New job posted",
        "body": "...",
    })
"""

import logging

from asgiref.sync import async_to_sync

from .sio import sio, _room_name

logger = logging.getLogger('notifications')


def broadcast_to_tag(tag: str, payload: dict) -> bool:
    """
    Sends `payload` to every socket currently subscribed to `tag`.
    Returns True if the message was handed off to Socket.IO (does NOT
    guarantee a client actually received it — that's normal for
    "best effort" realtime push).
    """
    return _emit_to_tag('notification', tag, payload)


def broadcast_update_to_tag(tag: str, payload: dict) -> bool:
    """
    Same as broadcast_to_tag, but tells clients this is a REVISION of a
    notification they may already be showing (edited title/body/image/
    regarding etc). Client should find the card by notification_id and
    update it in place rather than appending a new one.
    """
    return _emit_to_tag('notification_updated', tag, payload)


def broadcast_delete_to_tag(tag: str, notification_id: int, reason: str = '') -> bool:
    """
    Tells every client subscribed to `tag` to remove notification_id from
    their live feed right now. Used for:
      - a straight-up delete
      - "sent to the wrong tag" — pulling it back out of that tag's feed
        (you'd normally follow this up with a fresh broadcast_to_tag call
        on the correct tag)
    """
    payload = {"notification_id": notification_id, "reason": reason}
    return _emit_to_tag('notification_deleted', tag, payload)


def broadcast_status_to_tag(tag: str, payload: dict) -> bool:
    """
    Pushes delivery/seen-count style updates (success_count, failure_count,
    delivered_count, seen_count, ...) for a notification that's already on
    screen, without re-sending the full content.
    """
    return _emit_to_tag('notification_status_update', tag, payload)


def _emit_to_tag(event: str, tag: str, payload: dict) -> bool:
    payload = {**payload, "tag": tag}
    try:
        # async_to_sync is required because sio.emit() is async,
        # but Django views / most of your existing code is sync.
        async_to_sync(sio.emit)(event, payload, room=_room_name(tag))
    except Exception:
        logger.exception("Broadcast failed | event=%s | tag=%s", event, tag)
        return False

    logger.info("Broadcast sent | event=%s | tag=%s | payload_keys=%s",
                event, tag, list(payload.keys()))
    return True