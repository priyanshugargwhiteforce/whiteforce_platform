"""
Socket.IO server for real-time, tag-based notifications.

CONCEPT
───────
A "tag" is a Socket.IO "room" — e.g. "user_123", "branch_5", "job_alerts".
When a client connects, it tells us which tags it wants to listen to.
We put that socket into a room per tag. Whenever your Django view (or any
backend code) calls `broadcast_to_tag(tag, payload)`, every socket in that
room receives the message instantly.

CONNECTION (client side, JS example)
─────────────────────────────────────
    import { io } from "socket.io-client";

    const socket = io("https://astro-buddy.in", {
        path: "/socket.io/",
        auth: { api_key: "<API_SECRET_KEY>" },
        query: { tags: "user_123,job_alerts" },
    });

    socket.on("notification", (payload) => { ... });
    socket.on("connected", (data) => console.log(data.subscribed_tags));

    socket.emit("subscribe", { tags: ["branch_5"] });
    socket.emit("unsubscribe", { tags: ["branch_5"] });

SERVER → CLIENT EVENTS
───────────────────────
    "connected"               {"subscribed_tags": [...]}
    "notification"            new notification pushed to a tag room.
                              {"tag": ..., "notification_id": ..., "domain": ...,
                               "title": ..., "body": ..., "regarding": ...,
                               "html_content": ..., "image_url": ..., "created_at": ...}
    "notification_updated"    an existing notification's content changed
                              (edit). Same shape as "notification" — find
                              the card by notification_id and replace it.
    "notification_deleted"    a notification should be removed from the
                              live feed right now (delete, or "pull back"
                              from a tag it was mistakenly sent to).
                              {"tag": ..., "notification_id": ..., "reason": ...}
    "notification_status_update"  delivery/seen counters changed for a
                              notification already on screen (no need to
                              re-render content, just update counts).
                              {"tag": ..., "notification_id": ...,
                               "success_count": ..., "failure_count": ...,
                               "delivered_count": ..., "seen_count": ...}
    "subscribed"     {"subscribed_tags": [...]}
    "unsubscribed"   {"subscribed_tags": [...]}
    "error"          {"message": ...}

CLIENT → SERVER EVENTS
────────────────────────
    "subscribe"      {"tags": ["branch_5"]}
    "unsubscribe"    {"tags": ["branch_5"]}
"""

import logging
import os

import socketio
from django.conf import settings

logger = logging.getLogger('notifications')

MAX_TAGS_PER_CONNECTION = 50  # safety cap so one client can't subscribe to everything

# REDIS_URL is only used if you explicitly opt in (see below). Defaults to
# empty so local single-process dev never tries to reach a Redis server
# that doesn't exist.
REDIS_URL = os.environ.get('REDIS_URL', '')

# The manager is what lets multiple worker processes (e.g. several uvicorn
# workers, or multiple servers) all broadcast to the same rooms. With a
# single worker process (no --workers flag, i.e. `uvicorn ... ` with no
# --workers N) you don't need Redis at all — the in-memory manager works
# fine for local dev. Set REDIS_URL and USE_REDIS_SOCKETIO=True in your
# .env once you actually run multiple workers/processes (production).
if REDIS_URL and os.environ.get('USE_REDIS_SOCKETIO', 'False') == 'True':
    mgr = socketio.AsyncRedisManager(REDIS_URL)
else:
    mgr = None  # in-memory, single-process only — fine for local dev

sio = socketio.AsyncServer(
    async_mode='asgi',
    client_manager=mgr,
    cors_allowed_origins='*',   # tighten in production; see settings note below
)

# In-memory map of sid -> set of subscribed tags, used only for bookkeeping
# (so we can report "subscribed_tags" back to the client and know what to
# clean up). Room membership itself lives in the manager (Redis or
# in-memory), not here.
_sid_tags: dict[str, set[str]] = {}


def _room_name(tag: str) -> str:
    """Mirrors the old Channels group-name sanitizer."""
    safe = ''.join(c if c.isalnum() or c in '-_.' else '_' for c in tag.strip())
    return f"tag_{safe}"[:100]


@sio.event
async def connect(sid, environ, auth):
    # ── 1) Authenticate (same shared API key as the REST API) ──────────
    api_key = (auth or {}).get('api_key', '') if auth else ''
    if not api_key:
        # fall back to query string, for clients that can't send `auth`
        from urllib.parse import parse_qs
        qs = parse_qs(environ.get('QUERY_STRING', ''))
        api_key = qs.get('api_key', [''])[0]

    expected = getattr(settings, 'API_SECRET_KEY', '')
    if not expected or api_key != expected:
        logger.warning("SIO connect rejected | bad/missing api_key | sid=%s", sid)
        raise socketio.exceptions.ConnectionRefusedError('unauthorized')

    # ── 2) Read initial tags from query string ──────────────────────────
    from urllib.parse import parse_qs
    qs = parse_qs(environ.get('QUERY_STRING', ''))
    raw_tags = qs.get('tags', [''])[0]
    tags = [t.strip() for t in raw_tags.split(',') if t.strip()][:MAX_TAGS_PER_CONNECTION]

    _sid_tags[sid] = set()
    for tag in tags:
        await _subscribe(sid, tag)

    logger.info("SIO connected | tags=%s | sid=%s", tags, sid)
    await sio.emit('connected', {'subscribed_tags': sorted(_sid_tags[sid])}, to=sid)


@sio.event
async def disconnect(sid):
    # Leave every room we joined, so this dead socket doesn't get
    # messages broadcast to it anymore.
    for tag in list(_sid_tags.get(sid, [])):
        await sio.leave_room(sid, _room_name(tag))
    _sid_tags.pop(sid, None)
    logger.info("SIO disconnected | sid=%s", sid)


@sio.event
async def subscribe(sid, data):
    logger.info("SIO subscribe received | sid=%s | data=%s", sid, data)
    tags = (data or {}).get('tags', [])
    for tag in tags:
        if len(_sid_tags.get(sid, set())) >= MAX_TAGS_PER_CONNECTION:
            break
        await _subscribe(sid, tag)
    logger.info("SIO subscribe done | sid=%s | now_subscribed=%s | room=%s",
                sid, _sid_tags.get(sid), [_room_name(t) for t in _sid_tags.get(sid, [])])
    await sio.emit('subscribed', {'subscribed_tags': sorted(_sid_tags.get(sid, []))}, to=sid)


@sio.event
async def unsubscribe(sid, data):
    tags = (data or {}).get('tags', [])
    for tag in tags:
        await _unsubscribe(sid, tag)
    await sio.emit('unsubscribed', {'subscribed_tags': sorted(_sid_tags.get(sid, []))}, to=sid)


# ── Helpers ──────────────────────────────────────────────────────────────
async def _subscribe(sid, tag):
    if not tag or tag in _sid_tags.get(sid, set()):
        return
    await sio.enter_room(sid, _room_name(tag))
    _sid_tags.setdefault(sid, set()).add(tag)


async def _unsubscribe(sid, tag):
    if tag not in _sid_tags.get(sid, set()):
        return
    await sio.leave_room(sid, _room_name(tag))
    _sid_tags[sid].discard(tag)