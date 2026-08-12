"""
Thin wrapper around firebase_admin so the rest of the codebase never has
to think about *which* Firebase project a request belongs to -- it just
asks for the app registered under a given "domain" string ('jobs' or
'attendance') and gets back a ready-to-use firebase_admin App instance.

Why a separate firebase_admin App per domain?
Each Android app (white-force-jobs, white-force-attendance-app) is a
*different* Firebase project with its own service account credentials.
firebase_admin lets you run multiple named apps side-by-side in one
process via initialize_app(cred, name=...), which is exactly what we
need here.
"""

import os
import firebase_admin
from firebase_admin import credentials
from django.conf import settings


_initialized_apps = {}


def get_firebase_app(domain: str):
    """Lazily initializes (once) and returns the firebase_admin App for
    the given domain ('jobs' or 'attendance')."""

    if domain in _initialized_apps:
        return _initialized_apps[domain]

    cred_path = settings.FCM_CREDENTIALS.get(domain)
    if not cred_path:
        raise ValueError(f"No FCM credential path configured for domain '{domain}'.")

    if not os.path.exists(cred_path):
        raise FileNotFoundError(
            f"Service account JSON for domain '{domain}' not found at: {cred_path}\n"
            f"Download it from Firebase Console -> Project Settings -> Service "
            f"Accounts -> Generate new private key, and place it at that path "
            f"(or point FCM_CRED_{domain.upper()} env var to it)."
        )

    # Re-use an already-registered app of the same name if Django's autoreloader
    # re-imports this module (avoids "app already exists" errors in dev).
    try:
        app = firebase_admin.get_app(name=domain)
    except ValueError:
        cred = credentials.Certificate(cred_path)
        app = firebase_admin.initialize_app(cred, name=domain)

    _initialized_apps[domain] = app
    return app
