# whiteforce_platform

Ek single Django project jo multiple services ko assemble karta hai.
Sirf ek `manage.py`, ek `.env`, ek server — aur sab kuch chal jaata hai.

---

## Project Structure

```
whiteforce_platform/
├── core/                        ← Global Django settings & URLs
│   ├── settings.py
│   ├── urls.py
│   ├── wsgi.py
│   └── asgi.py
├── tallyapp/                    ← Tally Prime XML integration
├── config/                      ← Tally API config/models
├── notifications/               ← FCM push notifications (Whiteforce)
├── credentials/                 ← Firebase service account JSONs (gitignored)
├── tally_config.py              ← Tally instances config (edit URLs here)
├── .env                         ← All environment variables
├── requirements.txt
└── manage.py
```

---

## URL Routes

| Prefix                           | Service                        |
|----------------------------------|-------------------------------|
| `/admin/`                        | Django Admin                   |
| `/python/api/...`                | Tally API (default instance)   |
| `/python/<tally_id>/api/...`     | Tally API (specific instance)  |
| `/api/<domain>/notifications/...`| FCM Push Notifications         |

### Tally Endpoints
```
GET  /python/api/tally-instances/
GET  /python/<id>/api/ledgers/
GET  /python/<id>/api/ledgers/<name>/vouchers/
GET  /python/<id>/api/voucher/<master_id>/breakdown/
GET  /python/<id>/api/pending-bills/
GET  /python/<id>/api/all-ledgers-vouchers/
```

### Notification Endpoints
```
POST /api/<domain>/notifications/create/
POST /api/<domain>/notifications/send/
POST /api/<domain>/notifications/broadcast/
POST /api/<domain>/notifications/<id>/update/
POST /api/<domain>/notifications/<id>/delete/
```
- `<domain>` = `jobs` or `attendance`
- Header required: `X-API-KEY: <your_key>`

### Real-time (Socket.IO)
```
wss://astro-buddy.in/socket.io/
```
- Client connects with `auth: { api_key: "<API_SECRET_KEY>" }` and
  `query: { tags: "user_123,job_alerts" }` (comma-separated tags/rooms).
- Events pushed to subscribed tags: `notification`, `notification_updated`,
  `notification_deleted`, `notification_status_update`.
- See `notifications/sio.py` for the full event reference.

---

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill .env
cp .env.example .env   # edit values

# 3. Add Firebase credentials
# Place service account JSONs in credentials/
# (or set FCM_CRED_JOBS / FCM_CRED_ATTENDANCE in .env)

# 4. Run migrations
python manage.py migrate

# 5. Start server
# IMPORTANT: use uvicorn, NOT `manage.py runserver` -- runserver does not
# serve Socket.IO (real-time push). uvicorn serves both HTTP + Socket.IO
# through core/asgi.py.
uvicorn core.asgi:application --host 0.0.0.0 --port 8000

# For production with multiple workers, also set in .env:
#   REDIS_URL=redis://127.0.0.1:6379/1
#   USE_REDIS_SOCKETIO=True
# so all worker processes share Socket.IO room state via Redis.
```

---

## Tally Instances Config

Edit `tally_config.py` to add/change Tally URLs:

```python
TALLY_INSTANCES = {
    1: {
        "name":     "Office",
        "url":      "https://your-ngrok-url.ngrok-free.dev",
        "company":  "Company 1",
        "location": "Main Office",
    },
    # Add more instances here...
}
```

---

## Adding a New Project

1. Copy your Django app folder here (e.g., `email_validator/`)
2. Add it to `INSTALLED_APPS` in `core/settings.py`
3. Add its URL prefix in `core/urls.py`:
   ```python
   path('email/', include('email_validator.urls')),
   ```
4. Done. Migrate if new models.
