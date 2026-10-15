"""
GEMINI API KEY POOL (automatic failover)
-----------------------------------------
Shared by services/gemini_extractor.py (single parse) and
services/gemini_match.py (JD extraction, resume parsing and matching).

How it works:
  - Keys come from settings.GEMINI_API_KEYS (GEMINI_API_KEY_1, _2, _3 in .env).
  - Key #1 is always tried first. If Google answers 429 (rate limit / quota),
    that key is put on cooldown and the SAME request is immediately retried
    on key #2, then #3.
  - A key on cooldown is skipped until its cooldown ends, then used again.
  - Invalid / leaked / suspended keys (400 API_KEY_INVALID, 403) are parked
    for a long time so they don't waste a call on every request.
  - If every key is cooling down and the soonest one frees up within
    settings.GEMINI_MAX_WAIT seconds, the call sleeps once and retries;
    otherwise GeminiKeysUnavailable is raised so the caller can retry later.
  - Only the key INDEX is ever logged, never the key itself.

State is in memory, so each Celery worker process keeps its own cooldowns.
"""
import logging
import re
import threading
import time

import requests
from django.conf import settings

logger = logging.getLogger('bulkresume')

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_RETRY_DELAY_RE = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')


class GeminiKeysUnavailable(Exception):
    """Every configured Gemini key is cooling down (rate-limited or invalid)."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _cfg(name: str, default):
    return getattr(settings, name, default)


class GeminiKeyPool:
    def __init__(self, keys: list[str]):
        self._keys = list(keys)
        self._blocked_until = [0.0] * len(self._keys)
        self._lock = threading.Lock()

    # ── internal helpers ────────────────────────────────────────────────────
    def _pick(self, tried: set[int]) -> int | None:
        """First key (in .env order) that is not cooling down and not already
        tried during this call."""
        now = time.time()
        with self._lock:
            for i in range(len(self._keys)):
                if i not in tried and self._blocked_until[i] <= now:
                    return i
        return None

    def _soonest_free_in(self) -> float | None:
        now = time.time()
        with self._lock:
            if not self._blocked_until:
                return None
            return max(0.0, min(self._blocked_until) - now)

    def _block(self, idx: int, seconds: float, reason: str) -> None:
        with self._lock:
            self._blocked_until[idx] = max(self._blocked_until[idx], time.time() + seconds)
        logger.warning(
            f"Gemini key #{idx + 1}/{len(self._keys)} {reason} -- "
            f"cooling down {int(seconds)}s, switching to next key"
        )

    @staticmethod
    def _classify(response: requests.Response) -> tuple[float, str] | None:
        """None -> response is usable by the caller (success OR a non-key error).
        Otherwise (cooldown_seconds, reason) -> rotate to the next key."""
        code = response.status_code
        if code == 200:
            return None

        body = response.text or ""

        if code == 429:
            low = body.lower()
            if "perday" in low or "per day" in low:
                return float(_cfg('GEMINI_DAILY_COOLDOWN', 3600)), "hit its DAILY quota"
            match = _RETRY_DELAY_RE.search(body)
            if match:
                seconds = min(max(float(match.group(1)) + 2, 5.0), 300.0)
            else:
                seconds = float(_cfg('GEMINI_KEY_COOLDOWN', 65))
            return seconds, "is rate-limited (429)"

        if code == 403 or (code == 400 and ("API_KEY_INVALID" in body or "API key not valid" in body)):
            return float(_cfg('GEMINI_INVALID_KEY_COOLDOWN', 3600)), f"was rejected (HTTP {code}: invalid/leaked/no permission)"

        return None

    # ── public API ──────────────────────────────────────────────────────────
    def post(self, payload: dict, *, model: str, timeout: float, label: str = "") -> tuple[requests.Response, int]:
        """POST to generateContent, rotating keys on 429/invalid-key.
        Returns (response, key_number_used). Network errors (ConnectionError /
        Timeout) are NOT rotated -- they propagate to the caller's own retry."""
        if not self._keys:
            raise RuntimeError(
                "No Gemini API keys configured -- set GEMINI_API_KEY_1, GEMINI_API_KEY_2, ... in .env"
            )

        url = GEMINI_API_URL.format(model=model)
        max_wait = float(_cfg('GEMINI_MAX_WAIT', 30))
        tried: set[int] = set()
        waited = False

        while True:
            idx = self._pick(tried)

            if idx is None:
                wait = self._soonest_free_in()
                if not waited and wait is not None and wait <= max_wait:
                    logger.warning(
                        f"Gemini {label}: all keys cooling down, soonest frees in {wait:.0f}s -- waiting"
                    )
                    time.sleep(wait + 0.5)
                    waited = True
                    tried.clear()
                    continue
                raise GeminiKeysUnavailable(
                    f"All {len(self._keys)} Gemini keys are rate-limited/unavailable",
                    retry_after=wait,
                )

            # Key goes in a header, not the URL, so it never lands in logs/tracebacks.
            response = requests.post(
                url, headers={"x-goog-api-key": self._keys[idx]}, json=payload, timeout=timeout
            )

            verdict = self._classify(response)
            if verdict is None:
                if tried:
                    logger.info(f"Gemini {label}: succeeded on key #{idx + 1} after {len(tried)} failed key(s)")
                return response, idx + 1

            seconds, reason = verdict
            logger.warning(f"Gemini {label} | status={response.status_code} | body={response.text[:300]}")
            self._block(idx, seconds, reason)
            tried.add(idx)

    def status(self) -> list[dict]:
        """For debugging: availability of each key (index only, never the key)."""
        now = time.time()
        with self._lock:
            return [
                {
                    "key": i + 1,
                    "available": self._blocked_until[i] <= now,
                    "cooldown_left_s": max(0, int(self._blocked_until[i] - now)),
                }
                for i in range(len(self._keys))
            ]


_pool: GeminiKeyPool | None = None
_pool_lock = threading.Lock()


def get_gemini_pool() -> GeminiKeyPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            keys = list(_cfg('GEMINI_API_KEYS', []) or [])
            if not keys and _cfg('GEMINI_API_KEY', ''):
                keys = [settings.GEMINI_API_KEY]
            _pool = GeminiKeyPool(keys)
            logger.info(f"Gemini key pool initialised with {len(keys)} key(s)")
        return _pool