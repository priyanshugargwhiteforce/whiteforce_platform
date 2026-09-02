import logging

import requests
from django.conf import settings

logger = logging.getLogger('bulkresume')


class DedupCheckError(Exception):
    """Raised when the duplicate-check API call itself fails (network, 5xx, etc.)."""
    pass


def check_duplicate_in_db(email: str, phone: str) -> bool:
    """
    Calls the White Force "check-candidate-exists" API to check whether
    this email or phone already exists in the candidates database.

    API docs:
        POST https://white-force.com/plus/api/check-candidate-exists
        Body: {"phone": "...", "email": "..."}
        Public/open — no auth required.

    Returns:
        True  -> candidate already exists (skip LLM parsing)
        False -> candidate does not exist (go ahead and parse with LLM)

    Raises:
        DedupCheckError if the API call fails. Callers should decide whether
        to fail open (treat as "not duplicate", parse anyway) or fail closed.
    """
    if not email and not phone:
        # Nothing to check against — treat as not-a-duplicate so it still
        # goes through LLM extraction (regex just couldn't find anything).
        return False

    api_url = settings.DUPLICATE_CHECK_API_URL

    if not api_url:
        # Feature not configured yet — behave exactly like before (no skip).
        logger.debug("DUPLICATE_CHECK_API_URL not set; skipping dedup check.")
        return False

    payload = {}
    if phone:
        payload["phone"] = phone
    if email:
        payload["email"] = email

    try:
        response = requests.post(api_url, json=payload, timeout=8)
        # API returns 404 for "not found" and 422 for missing params —
        # both are valid, parseable JSON responses, not transport failures.
        # Only raise for real server errors (5xx) or connection issues.
        if response.status_code >= 500:
            response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logger.warning(f"Duplicate-check API call failed: {exc}")
        raise DedupCheckError(str(exc)) from exc
    except ValueError as exc:  # bad JSON
        logger.warning(f"Duplicate-check API returned invalid JSON: {exc}")
        raise DedupCheckError(str(exc)) from exc

    exists = bool(data.get("exists"))
    matched_by = data.get("matched_by")

    logger.info(
        f"Dedup check for email={email!r} phone={phone!r} "
        f"-> exists={exists} matched_by={matched_by}"
    )
    return exists