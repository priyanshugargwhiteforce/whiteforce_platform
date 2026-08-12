"""
Custom throttle classes for the two notification endpoints.

Rates are configured in settings.py → REST_FRAMEWORK → DEFAULT_THROTTLE_RATES.
The cache backend defined in settings.CACHES['default'] stores the counters.

To change limits without touching code, just update settings:
    'create_notification': '60/minute'
    'send_notification':   '10/hour'
"""

import logging
from rest_framework.throttling import AnonRateThrottle

logger = logging.getLogger('notifications')


class _LoggedThrottle(AnonRateThrottle):
    """Base class that logs when a request is throttled."""

    def allow_request(self, request, view):
        allowed = super().allow_request(request, view)
        if not allowed:
            logger.warning(
                "Rate limit exceeded | scope=%s | ip=%s | path=%s",
                self.scope,
                self.get_ident(request),
                request.path,
            )
        return allowed

    def get_cache_key(self, request, view):
        # Key by IP address (works for both authenticated and anonymous calls)
        ident = self.get_ident(request)
        return self.cache_format % {'scope': self.scope, 'ident': ident}


class CreateNotificationThrottle(_LoggedThrottle):
    scope = 'create_notification'


class SendNotificationThrottle(_LoggedThrottle):
    scope = 'send_notification'

class ValidateTokensThrottle(_LoggedThrottle):
    scope = 'validate_tokens'

class SendWiraThrottle(CreateNotificationThrottle):
    scope = 'send_wira'