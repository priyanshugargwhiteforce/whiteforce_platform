"""
Tolerant coercion of raw LLM JSON into our Pydantic schemas.

Every schema in schemas.py uses extra="forbid" with plain str/float/bool
fields, so ONE wrong key ("pincode/postal_code", "percentage"), one `null`,
or one "85%" used to throw away the ENTIRE extraction. coerce_to_model()
normalizes the raw JSON (renames near-miss keys, coerces types, folds
unknown-but-useful keys into a field that exists) BEFORE validation.
Lives outside schemas.py so the single-parse path is untouched.
"""
import re
from typing import Union, get_args, get_origin

TOP_LEVEL_ALIASES = {
    "pincode": "pincode_postal_code", "pin_code": "pincode_postal_code",
    "postal_code": "pincode_postal_code", "zip_code": "pincode_postal_code",
    "pincode_postal": "pincode_postal_code",
    "languages": "known_languages", "summary": "profile_summary",
    "address": "candidate_address", "linkedin": "linkedin_url",
}

# Extra keys a model tends to add to a nested object -> where that info goes
# instead of being dropped.
_ITEM_KEY_ALIASES = {
    "tech_stack": "methodologies", "technologies": "methodologies",
    "tools": "methodologies", "tech": "methodologies",
    "role": "title", "position": "title", "designation": "title",
    "organization": "company", "organisation": "company", "employer": "company",
    "client": "company", "school": "institution", "university": "institution",
    "college": "institution", "provider_name": "provider", "issuer": "provider",
    "from": "start_date", "to": "end_date", "start": "start_date", "end": "end_date",
    "period": "duration", "responsibilities": "description", "details": "description",
    # marks column of an education row -> Education.percentage
    "percent": "percentage", "cgpa": "percentage", "gpa": "percentage", "grade": "percentage",
    "score": "percentage", "marks": "percentage", "result": "percentage",
}

# Other facts about a qualification (table columns like BOARD / STREAM) are
# folded into `degree` -- e.g. "HSC (Maharashtra Board)" -- so that info is
# kept without needing its own schema field.
_EDU_EXTRA_KEYS = {"board", "stream", "specialization", "specialisation", "category",
                   "field", "sector"}


def _norm_key(key) -> str:
    return re.sub(r'[^a-z0-9]+', '_', str(key).lower()).strip('_')


def _as_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(s for s in (_as_str(v) for v in value) if s)
    if isinstance(value, dict):
        return ", ".join(s for s in (_as_str(v) for v in value.values()) if s)
    return str(value)


def _as_str_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        items = re.split(r'[,;\n|•]+', value)
    elif isinstance(value, (list, tuple)):
        items = [_as_str(v) for v in value]
    else:
        items = [_as_str(value)]
    seen, out = set(), []
    for item in items:
        item = item.strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    return str(value).strip().lower() in ("true", "yes", "y", "1", "matched", "match")


def _as_float(value) -> float:
    if isinstance(value, bool):
        return 100.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    found = re.search(r'-?\d+(?:\.\d+)?', str(value or ''))
    return float(found.group()) if found else 0.0


def _field_kind(annotation):
    """Classify a Pydantic field annotation: str / bool / float / list of str /
    list of nested models. Returns (kind, nested_model_or_None)."""
    if annotation is str:
        return "str", None
    if get_origin(annotation) is Union and set(get_args(annotation)) == {str, type(None)}:
        return "opt_str", None   # Optional[str]: None when absent, never ""
    if annotation is bool:
        return "bool", None
    if annotation is float:
        return "float", None
    if get_origin(annotation) is list:
        (inner,) = get_args(annotation)
        if inner is str:
            return "list_str", None
        return "list_obj", inner
    return "other", None


def _coerce_value(kind: str, nested, value, field_name: str):
    if kind == "str":
        return _as_str(value)
    if kind == "opt_str":
        text = _as_str(value)
        return text if text and text.lower() not in ("null", "none", "n/a", "na", "-") else None
    if kind == "bool":
        return _as_bool(value)
    if kind == "float":
        number = _as_float(value)
        return min(max(number, 0.0), 100.0) if field_name.endswith("percent") else number
    if kind == "list_str":
        return _as_str_list(value)
    if kind == "list_obj":
        if isinstance(value, dict):
            value = [value]
        elif not isinstance(value, (list, tuple)):
            value = [] if value in (None, "") else [value]
        cleaned = (_clean_item(item, nested) for item in value)
        return [c for c in cleaned if c]
    return value


def _clean_item(item, model):
    """Normalize one nested object (an education row, a project, a field_breakdown
    row...) to exactly `model`'s fields. Returns None if nothing usable is left."""
    fields = model.model_fields
    if isinstance(item, str):
        item = {next(iter(fields)): item}
    if not isinstance(item, dict):
        return None

    out, extras = {}, []
    for raw_key, value in item.items():
        key = _norm_key(raw_key)
        if key not in fields:
            key = _ITEM_KEY_ALIASES.get(key, key)
        if key in fields:
            kind, nested = _field_kind(fields[key].annotation)
            coerced = _coerce_value(kind, nested, value, key)
            if key in out and out[key] not in ("", [], None):
                # Two source keys mapped to one field (e.g. tools + tech_stack): merge lists.
                if isinstance(coerced, list):
                    out[key] = _as_str_list(list(out[key]) + coerced)
                continue
            out[key] = coerced
        elif key in _EDU_EXTRA_KEYS and "degree" in fields:
            text = _as_str(value)
            if text:
                extras.append(text)
        # Any other unknown key is dropped instead of failing the whole parse.

    if extras and "degree" in fields:
        base = out.get("degree", "")
        out["degree"] = f"{base} ({', '.join(extras)})" if base else ", ".join(extras)

    if not any(v not in ("", [], None, False, 0.0) for v in out.values()):
        return None
    return out


def coerce_to_model(raw, model, aliases: dict | None = None) -> dict:
    """Turn whatever JSON object the model produced into a dict that will
    validate against `model`: unknown keys dropped/renamed, nulls -> ""/[],
    strings <-> lists, numbers/bools parsed, nested rows cleaned."""
    if not isinstance(raw, dict):
        raise ValueError(f"model output is not a JSON object: {type(raw).__name__}")

    fields = model.model_fields
    cleaned = {}
    for raw_key, value in raw.items():
        key = _norm_key(raw_key)
        if key not in fields and aliases:
            key = aliases.get(key, key)
        if key not in fields:
            continue
        kind, nested = _field_kind(fields[key].annotation)
        coerced = _coerce_value(kind, nested, value, key)
        if key in cleaned and cleaned[key] not in ("", [], None):
            continue
        cleaned[key] = coerced
    return cleaned

