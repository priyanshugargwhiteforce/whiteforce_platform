import logging
import re

import requests
from django.conf import settings

logger = logging.getLogger('bulkresume')


class DedupCheckError(Exception):
    """Raised when the duplicate-check API call itself fails (network, 5xx, etc.)."""
    pass


def _normalize_phone(phone: str) -> str:
    """Keep digits only; drop +91 / 91 / 0 prefix so we send a clean 10-digit number."""
    digits = re.sub(r'\D', '', phone or '')
    if len(digits) > 10:
        digits = digits[-10:]
    return digits


def check_duplicate_in_db(email: str, phone: str) -> bool:
    """
    Calls the White Force "check-candidate-exists" API with the candidate's
    mobile number. The API only supports mobile lookup (email-only requests
    return 422), so `email` is kept in the signature for compatibility but
    is not used for the lookup.

    Response shape:
        {"status": true, "found_count": 1,
         "data": [{"mobile": "...", "exists": true, "source": "candidates", "data": {...}}]}

    Returns:
        True  -> candidate already exists (skip LLM parsing)
        False -> candidate does not exist (go ahead and parse)

    Raises:
        DedupCheckError if the API call fails (network / 5xx / unexpected shape).
        Callers fail OPEN (parse anyway).
    """
    mobile = _normalize_phone(phone)
    if len(mobile) != 10:
        # No usable phone number found by regex -> can't check, so parse.
        logger.info(f"Dedup check skipped: no valid phone (email={email!r} phone={phone!r})")
        return False

    api_url = settings.DUPLICATE_CHECK_API_URL
    if not api_url:
        logger.debug("DUPLICATE_CHECK_API_URL not set; skipping dedup check.")
        return False

    payload = {"phone": mobile}

    try:
        response = requests.post(api_url, json=payload, timeout=8)
        if response.status_code >= 500:
            response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logger.warning(f"Duplicate-check API call failed: {exc}")
        raise DedupCheckError(str(exc)) from exc
    except ValueError as exc:  # bad JSON
        logger.warning(f"Duplicate-check API returned invalid JSON: {exc}")
        raise DedupCheckError(str(exc)) from exc

    if response.status_code != 200 or not data.get("status"):
        # e.g. 422 validation error -> don't silently treat as "not duplicate"
        logger.warning(f"Dedup API unexpected response: status={response.status_code} body={data}")
        raise DedupCheckError(f"Unexpected dedup API response: {data.get('message')}")

    results = data.get("data") or []
    exists = any(item.get("exists") for item in results) or (data.get("found_count", 0) > 0)

    matched = results[0].get("data", {}) if results and results[0].get("exists") else {}
    logger.info(
        f"Dedup check for phone={mobile!r} (email={email!r}) -> exists={exists} "
        f"matched_candidate_id={matched.get('id')}"
    )
    return exists