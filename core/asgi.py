"""
ASGI config for whiteforce_platform project.

This routes BOTH protocols: python-socketio wraps the Django ASGI app
directly -- any request to /socket.io/... is handled by Socket.IO,
everything else falls through to Django (tallyapp + notifications REST
views).
"""

import os

import socketio
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')

# IMPORTANT: get_asgi_application() must be called BEFORE importing
# anything that touches Django models/apps (like notifications.sio below),
# otherwise you'll hit "Apps aren't loaded yet" errors.
django_asgi_app = get_asgi_application()

from notifications.sio import sio  # noqa: E402

application = socketio.ASGIApp(
    sio,
    other_asgi_app=django_asgi_app,
    socketio_path='socket.io',
)
