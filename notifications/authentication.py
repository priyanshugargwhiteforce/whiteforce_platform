"""
Simple static API-key authentication.

Clients must include the header:
    X-API-KEY: <value of API_SECRET_KEY in settings / .env>

This intentionally uses a single shared key so your colleague's admin
console just needs one constant — no OAuth flows, no token rotation yet.
Swap for TokenAuthentication (DRF) or django-rest-knox later if needed.
"""

import logging
from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

logger = logging.getLogger('notifications')


class ApiKeyAuthentication(BaseAuthentication):
    HEADER = 'HTTP_X_API_KEY'       # Django converts X-API-KEY → HTTP_X_API_KEY

    def authenticate(self, request):
        api_key = request.META.get(self.HEADER, '').strip()

        if not api_key:
            # No key sent at all → let DRF handle as unauthenticated
            # (the IsAuthenticated permission class will then reject it)
            return None

        expected = getattr(settings, 'API_SECRET_KEY', '')

        if not expected:
            # Misconfiguration — warn loudly in logs
            logger.error(
                "API_SECRET_KEY is not set in settings/env. "
                "All API key checks will fail until it is configured."
            )
            raise AuthenticationFailed("Server misconfiguration: API key not configured.")

        if api_key != expected:
            # Log the attempt (first 8 chars only — never log full keys)
            logger.warning(
                "Invalid API key attempt | key_prefix=%s | ip=%s",
                api_key[:8] + '…',
                _get_client_ip(request),
            )
            raise AuthenticationFailed("Invalid or missing API key.")

        # DRF needs (user, auth) — we return a sentinel object for user
        # since we don't have real user accounts for this service.
        logger.debug("API key authenticated | ip=%s", _get_client_ip(request))
        return (_ApiKeyUser(), api_key)

    def authenticate_header(self, request):
        return 'X-API-KEY'


class _ApiKeyUser:
    """Minimal stand-in for request.user so DRF's IsAuthenticated passes."""
    is_authenticated = True

    def __str__(self):
        return 'api-key-user'


def _get_client_ip(request):
    x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded:
        return x_forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', 'unknown')