import json
import logging

import requests
from django.conf import settings

logger = logging.getLogger('bulkresume')

GET_PROFILE_DATA_API_URL = getattr(
    settings, 'GET_PROFILE_DATA_API_URL', 'https://white-force.com/plus/api/get-profile-data'
)
GET_PROFILE_DATA_API_KEY = getattr(settings, 'GET_PROFILE_DATA_API_KEY', '')


class ProfileLookupError(Exception):
    """Raised when the get-profile-data API call itself fails (network, 5xx, etc.)."""
    pass


def get_profile_by_phone(phone: str) -> dict | None:
    """
    Calls the White Force "get-profile-data" API to fetch an existing
    candidate's full profile by phone number, mirroring how
    services/dedup_check.py calls check-candidate-exists.

    API (confirmed from sample):
        GET https://white-force.com/plus/api/get-profile-data?mobile=<phone>
        Header: x-api-key: <GET_PROFILE_DATA_API_KEY>

        Success response:
            {"status": true, "message": "...", "data": {...flat candidate dict...}}
        Not-found response shape unconfirmed — assumed to be
        {"status": false, "message": "..."} based on the success shape's
        pattern. Tighten this once a real not-found sample is available.

    Returns:
        dict  -> the raw profile payload (the "data" object), if found
        None  -> no candidate found for this phone number

    Raises:
        ProfileLookupError on network/5xx/bad-JSON failures. Callers should
        fail OPEN (treat as "not found", parse the resume fresh) rather than
        fail the whole match just because this lookup API had a hiccup —
        same philosophy as DedupCheckError in services/dedup_check.py.
    """
    if not phone:
        return None

    try:
        response = requests.get(
            GET_PROFILE_DATA_API_URL,
            params={"mobile": phone},
            headers={"x-api-key": GET_PROFILE_DATA_API_KEY},
            timeout=8,
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 500:
            response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logger.warning(f"get-profile-data API call failed for phone={phone!r}: {exc}")
        raise ProfileLookupError(str(exc)) from exc
    except ValueError as exc:  # bad JSON
        logger.warning(f"get-profile-data API returned invalid JSON: {exc}")
        raise ProfileLookupError(str(exc)) from exc

    if not data or not isinstance(data, dict):
        return None

    if data.get("status") is not True or not isinstance(data.get("data"), dict):
        logger.info(f"get-profile-data: no profile found for phone={phone!r} ({data.get('message')!r})")
        return None

    logger.info(f"get-profile-data: profile found for phone={phone!r}")
    return data["data"]


def _parse_json_list(raw):
    """experience_details / education_details come back as JSON-encoded
    strings (not real arrays). Parse defensively — return [] on anything
    unexpected rather than blowing up downstream matching."""
    if isinstance(raw, list):
        return raw
    if not raw or not isinstance(raw, str):
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def _split_csv(raw):
    """skills / languages come back as comma-separated strings, not lists."""
    if isinstance(raw, list):
        return raw
    if not raw or not isinstance(raw, str):
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def normalize_db_profile(db_profile: dict) -> dict:
    """
    Maps a White Force DB profile payload onto the same field names
    services/schemas.py's ResumeExtraction uses, so downstream matching
    code (services/matcher.py) never needs to know whether a candidate's
    profile came from the DB or from a fresh resume parse.

    Confirmed against a real get-profile-data response. Notes:
      - `skills` and `languages` are comma-separated strings in the DB
        payload, not lists — split here.
      - `experience_details` / `education_details` are JSON-encoded
        strings, not real arrays — parsed here via _parse_json_list.
      - top-level `experience` is a yes/no "has experience" flag, not the
        experience list — real experience list is `experience_details`,
        real years-of-experience number is `total_experience`.
      - No `certifications`, `hobbies`, `training`, `linkedin_url`,
        `other_urls`, or `profile_summary` fields exist in this payload —
        left empty/default.
      - There's no separate `internships` field; internships would need to
        be inferred from experience_details entries if that's ever needed.
    """

    def first(*keys, default=""):
        for k in keys:
            v = db_profile.get(k)
            if v not in (None, ""):
                return v
        return default

    def first_list(*keys):
        for k in keys:
            v = db_profile.get(k)
            if isinstance(v, list):
                return v
        return []

    return {
        "name": first("name", "candidate_name", "full_name"),
        "email": first("email", "email_id"),
        "phone": first("mobile", "phone", "phone_number"),
        "gender": first("gender"),
        "date_of_birth": first("date_of_birth", "dob"),
        "marital_status": first("marital_status"),
        "father_name": first("father_name"),
        "mother_name": first("mother_name"),
        "known_languages": _split_csv(first("languages", "known_languages")) or first_list("known_languages", "languages"),
        "candidate_address": first("address", "candidate_address"),
        "pincode_postal_code": first("pin_code", "pincode", "postal_code"),
        "hobbies": first_list("hobbies"),
        "training": first_list("training"),
        "linkedin_url": first("linkedin_url", "linkedin"),
        "other_urls": first_list("other_urls"),
        "education": _parse_json_list(db_profile.get("education_details")) or first_list("education", "qualifications"),
        "experience": _parse_json_list(db_profile.get("experience_details")) or first_list("experience", "work_experience"),
        "skills": _split_csv(first("skills", "key_skills")) or first_list("skills", "key_skills"),
        "certifications": first_list("certifications"),
        "internships": first_list("internships"),
        "profile_summary": first("profile_summary", "summary"),
        "extraction_method": "db_profile",
        "parse_score": None,
    }