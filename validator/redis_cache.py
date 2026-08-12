"""
Redis Cache Layer for Email Validation
=======================================
- Caches full email validation results (TTL: 24 hours)
- Caches domain-level results: MX, SPF, DKIM, DMARC, reputation, age (TTL: 6 hours)
- Graceful fallback: if Redis is down, validation runs normally without crashing
"""

import json
import hashlib
import logging
from functools import wraps

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# TTL CONSTANTS  (seconds)
# ─────────────────────────────────────────────────────────
EMAIL_CACHE_TTL   = 86_400   # 24 hours  – full email result
DOMAIN_CACHE_TTL  = 21_600   #  6 hours  – MX / SPF / DKIM / DMARC / reputation / age
SMTP_CACHE_TTL    = 3_600    #  1 hour   – SMTP result (changes more often)

# ─────────────────────────────────────────────────────────
# REDIS CLIENT (lazy singleton)
# ─────────────────────────────────────────────────────────
_redis_client = None

def get_redis():
    """Return a Redis client, or None if unavailable."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        import redis
        from django.conf import settings
        cfg = getattr(settings, 'REDIS_CONFIG', {})
        _redis_client = redis.Redis(
            host=cfg.get('HOST', 'localhost'),
            port=cfg.get('PORT', 6379),
            db=cfg.get('DB', 0),
            password=cfg.get('PASSWORD', None),
            socket_connect_timeout=5,
            socket_timeout=5,
            decode_responses=True,
            retry_on_timeout=True,
        )
        _redis_client.ping()   # verify connection on first use
        logger.info("Redis connected successfully")
        return _redis_client
    except Exception as e:
        logger.warning(f"Redis unavailable, caching disabled: {e}")
        _redis_client = None
        return None


# ─────────────────────────────────────────────────────────
# KEY HELPERS
# ─────────────────────────────────────────────────────────
def _email_key(email: str) -> str:
    h = hashlib.md5(email.lower().strip().encode()).hexdigest()
    return f"ev:email:{h}"

def _domain_key(domain: str, check: str) -> str:
    return f"ev:domain:{domain.lower()}:{check}"

def _smtp_key(email: str) -> str:
    h = hashlib.md5(email.lower().strip().encode()).hexdigest()
    return f"ev:smtp:{h}"


# ─────────────────────────────────────────────────────────
# GENERIC GET / SET
# ─────────────────────────────────────────────────────────
def cache_get(key: str):
    r = get_redis()
    if r is None:
        return None
    try:
        val = r.get(key)
        return json.loads(val) if val is not None else None
    except Exception as e:
        logger.debug(f"cache_get error [{key}]: {e}")
        return None


def cache_set(key: str, value, ttl: int):
    r = get_redis()
    if r is None:
        return
    try:
        r.setex(key, ttl, json.dumps(value, default=str))
    except Exception as e:
        logger.debug(f"cache_set error [{key}]: {e}")


# ─────────────────────────────────────────────────────────
# PUBLIC API  – EMAIL RESULT
# ─────────────────────────────────────────────────────────
def get_cached_email_result(email: str):
    return cache_get(_email_key(email))

def set_cached_email_result(email: str, result: dict):
    cache_set(_email_key(email), result, EMAIL_CACHE_TTL)


# ─────────────────────────────────────────────────────────
# PUBLIC API  – DOMAIN CHECKS
# ─────────────────────────────────────────────────────────
def get_cached_domain(domain: str, check: str):
    return cache_get(_domain_key(domain, check))

def set_cached_domain(domain: str, check: str, value):
    cache_set(_domain_key(domain, check), value, DOMAIN_CACHE_TTL)


# ─────────────────────────────────────────────────────────
# PUBLIC API  – SMTP RESULT
# ─────────────────────────────────────────────────────────
def get_cached_smtp(email: str):
    return cache_get(_smtp_key(email))

def set_cached_smtp(email: str, result: dict):
    cache_set(_smtp_key(email), result, SMTP_CACHE_TTL)


# ─────────────────────────────────────────────────────────
# BATCH HELPERS
# ─────────────────────────────────────────────────────────
def get_many_email_results(emails: list) -> dict:
    """
    Pipeline-fetch cached results for multiple emails.
    Returns {email: result_or_None}.
    """
    r = get_redis()
    if r is None:
        return {e: None for e in emails}

    try:
        keys  = [_email_key(e) for e in emails]
        pipe  = r.pipeline()
        for k in keys:
            pipe.get(k)
        values = pipe.execute()

        result = {}
        for email, val in zip(emails, values):
            result[email] = json.loads(val) if val is not None else None
        return result
    except Exception as e:
        logger.debug(f"get_many_email_results error: {e}")
        return {e: None for e in emails}


def cache_stats() -> dict:
    """Return basic Redis info for monitoring."""
    r = get_redis()
    if r is None:
        return {"redis": "unavailable"}
    try:
        info = r.info("memory")
        return {
            "redis": "connected",
            "used_memory_human": info.get("used_memory_human"),
            "cached_keys": r.dbsize(),
        }
    except Exception:
        return {"redis": "error"}
