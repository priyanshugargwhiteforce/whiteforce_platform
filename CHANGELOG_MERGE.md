# whiteforce_platform — merge update (tally_api + whiteforce_notifications)

Ye file batati hai ki `whiteforce_notifications` aur `tally_api` ke latest code se
`whiteforce_platform` mein kya-kya update hua hai. **Server pe deploy karne se
pehle ye poora file padh lena.**

## Summary

- `tallyapp` (Tally XML integration) — koi file-level ya content-level change
  nahi mila. Woh already fully up-to-date tha platform mein. **No action needed.**
- `notifications` app — bahut peeche tha (Socket.IO real-time layer poora
  missing tha). Ab fully sync kar diya gaya hai latest `whiteforce_notifications`
  source ke saath.

---

## Files changed

### New files added
- `notifications/sio.py` — Socket.IO server (python-socketio AsyncServer).
  Tag-based rooms, connect/disconnect/subscribe/unsubscribe handlers,
  API-key auth on connect.
- `notifications/broadcast.py` — sync helper functions (`broadcast_to_tag`,
  `broadcast_update_to_tag`, `broadcast_delete_to_tag`, `broadcast_status_to_tag`)
  callable from normal Django views to push events over Socket.IO.

### Fully replaced (old version tha platform mein, bahut peeche)
- `notifications/models.py` — added `last_broadcast_tags` (JSONField, tracks
  which tag-rooms a notification was pushed to), `is_deleted` + `deleted_at`
  (soft delete), `updated_at` (edit tracking).
- `notifications/serializers.py` — added `NotificationUpdateSerializer` for
  the PATCH-style update endpoint.
- `notifications/urls.py` — added 3 new routes: `broadcast/`, `<id>/update/`,
  `<id>/delete/`.
- `notifications/admin.py` — added soft-delete admin action that also
  broadcasts a `notification_deleted` event to every tag the notification
  was last sent to; new list columns/filters for `is_deleted`.
- `notifications/views.py` — added `BroadcastNotificationView`,
  `UpdateNotificationView`, `DeleteNotificationView` (real-time
  update/delete dispatch over Socket.IO, soft-delete support).

### Project-level (core/)
- `core/asgi.py` — **rewritten**. Was plain `get_asgi_application()` only
  (no Socket.IO at all). Now wraps Django's ASGI app with
  `socketio.ASGIApp(sio, other_asgi_app=django_asgi_app, socketio_path='socket.io')`,
  same as the standalone notifications project. This is the single most
  important change — without it, real-time push does not work at all,
  even though the REST endpoints would still respond fine.
- `core/settings.py` — **not changed** (already correct: `ASGI_APPLICATION =
  'core.asgi.application'`, API-key auth, AllowAny default for tally).
  No Channels/channels_redis needed — this project uses python-socketio
  directly, not Django Channels.

### Config / deploy files
- `requirements.txt` — added `python-socketio==5.13.0`, `uvicorn==0.34.0`,
  `redis==5.2.1`. These were missing entirely; without them the app won't
  even import (`ModuleNotFoundError: No module named 'socketio'`).
- `.env` — added `REDIS_URL=redis://127.0.0.1:6379/1` and
  `USE_REDIS_SOCKETIO=True` (matches your VPS setup so multiple uvicorn
  workers share Socket.IO room state).
- `.env.example` — same two vars added, defaulted to `USE_REDIS_SOCKETIO=False`
  for local single-worker dev.
- `README.md` — updated endpoint table (added broadcast/update/delete +
  Socket.IO section), and changed the "start server" instructions from
  `python manage.py runserver` to `uvicorn core.asgi:application --host 0.0.0.0
  --port 8000` (runserver does NOT serve Socket.IO).

---

## Deploy steps on the VPS

```bash
git pull                                  # or however you sync the code
pip install -r requirements.txt           # picks up python-socketio, uvicorn, redis
python manage.py makemigrations notifications
python manage.py migrate                  # new fields: last_broadcast_tags,
                                           # is_deleted, deleted_at, updated_at
```

Then restart however you run it in production — if you're on
Gunicorn/systemd, switch that service to run uvicorn against
`core.asgi:application` instead of `core.wsgi:application` under Gunicorn's
sync worker, since Socket.IO needs an ASGI server. Something like:

```bash
uvicorn core.asgi:application --host 0.0.0.0 --port 8000 --workers 4
```

Make sure Redis is running and reachable at `REDIS_URL` before starting with
`USE_REDIS_SOCKETIO=True` and more than 1 worker — otherwise rooms won't be
shared across workers and clients connected to different workers won't see
each other's broadcasts.

Nginx reverse proxy config should already have a `/socket.io/` location with
`proxy_http_version 1.1` and the `Upgrade`/`Connection` headers set for
WebSocket upgrade — double check this is in place since this project
previously only served plain HTTP.
