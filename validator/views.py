from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse, StreamingHttpResponse
from django.conf import settings

from concurrent.futures import ThreadPoolExecutor, as_completed

import json
import csv
import time

from validator.services    import validate_email
from validator.redis_cache import get_many_email_results, set_cached_email_result, cache_stats


# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
VALIDATOR_CONFIG = getattr(settings, 'EMAIL_VALIDATOR', {})
MAX_WORKERS      = VALIDATOR_CONFIG.get('MAX_WORKERS', 50)
BATCH_MAX_EMAILS = VALIDATOR_CONFIG.get('BATCH_MAX_EMAILS', 500)


# ─────────────────────────────────────────────────────────────
# SAFE VALIDATION WRAPPER
# ─────────────────────────────────────────────────────────────
def safe_validate(email):
    try:
        result = validate_email(email)
        if not isinstance(result, dict):
            return {"email": email, "status": "error", "error": "Invalid validator response"}
        result.setdefault("email", email)
        return result
    except Exception as e:
        return {"email": email, "status": "error", "error": str(e)}


# ─────────────────────────────────────────────────────────────
# JSON PARSER
# ─────────────────────────────────────────────────────────────
def parse_json_body(request):
    try:
        body = request.body.decode("utf-8").strip()
        if not body:
            return None, JsonResponse({"success": False, "error": "Empty request body"}, status=400)
        data = json.loads(body)
        if not isinstance(data, dict):
            return None, JsonResponse({"success": False, "error": "JSON must be an object"}, status=400)
        return data, None
    except json.JSONDecodeError:
        return None, JsonResponse({"success": False, "error": "Invalid JSON format"}, status=400)
    except Exception as e:
        return None, JsonResponse({"success": False, "error": str(e)}, status=400)


# ─────────────────────────────────────────────────────────────
# BUILD SUMMARY
# ─────────────────────────────────────────────────────────────
def build_summary(results):
    summary = {"deliverable": 0, "risky": 0, "invalid": 0, "unknown": 0, "errors": 0}
    for r in results:
        s = str(r.get("status", "")).lower()
        if s == "deliverable":
            summary["deliverable"] += 1
        elif s == "risky":
            summary["risky"] += 1
        elif s in ("invalid", "undeliverable", "disposable", "typo_detected"):
            summary["invalid"] += 1
        elif s == "error":
            summary["errors"] += 1
        else:
            summary["unknown"] += 1
    return summary


# ─────────────────────────────────────────────────────────────
# SINGLE EMAIL API
# ─────────────────────────────────────────────────────────────
@csrf_exempt
def validate_email_api(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required"}, status=405)

    data, error = parse_json_body(request)
    if error:
        return error

    email = data.get("email")
    if not isinstance(email, str) or "@" not in email:
        return JsonResponse({"success": False, "error": "Valid email is required"}, status=400)

    result = safe_validate(email.strip().lower())
    return JsonResponse({"success": True, "result": result})


# ─────────────────────────────────────────────────────────────
# BATCH EMAIL API  (Redis-optimised)
# ─────────────────────────────────────────────────────────────
@csrf_exempt
def batch_validate_api(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required"}, status=405)

    start_time = time.time()
    emails = []

    try:
        # ── Parse input ──────────────────────────────────────
        if "file" in request.FILES:
            file    = request.FILES["file"]
            decoded = file.read().decode("utf-8", errors="ignore").splitlines()
            reader  = csv.reader(decoded)
            for row in reader:
                if row:
                    e = row[0].strip().lower()
                    if "@" in e:
                        emails.append(e)
        else:
            data, error = parse_json_body(request)
            if error:
                return error
            emails = data.get("emails", [])

        # ── Deduplicate & clean ───────────────────────────────
        cleaned = []
        for e in emails:
            if isinstance(e, str):
                e = e.strip().lower()
                if "@" in e:
                    cleaned.append(e)
        emails = list(set(cleaned))

        if not emails:
            return JsonResponse({"success": False, "error": "No valid emails found"}, status=400)

        if len(emails) > BATCH_MAX_EMAILS:
            return JsonResponse(
                {"success": False, "error": f"Maximum {BATCH_MAX_EMAILS} emails allowed"},
                status=400,
            )

        # ────────────────────────────────────────────────────────
        # REDIS BATCH CACHE LOOKUP
        #   Pipeline-fetch ALL emails in one round-trip.
        #   Only validate the ones that are NOT cached.
        # ────────────────────────────────────────────────────────
        cached_map = get_many_email_results(emails)

        results          = []
        cache_hits       = []
        emails_to_validate = []

        for email in emails:
            if cached_map.get(email) is not None:
                cache_hits.append(cached_map[email])
            else:
                emails_to_validate.append(email)

        results.extend(cache_hits)

        # ── Validate uncached emails with thread pool ─────────
        if emails_to_validate:
            workers = min(MAX_WORKERS, len(emails_to_validate))

            if len(emails_to_validate) < 20:
                # Sequential for tiny batches
                for email in emails_to_validate:
                    results.append(safe_validate(email))
            else:
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    future_map = {
                        executor.submit(safe_validate, email): email
                        for email in emails_to_validate
                    }
                    for future in as_completed(future_map):
                        email = future_map[future]
                        try:
                            result = future.result(timeout=15)
                            if not isinstance(result, dict):
                                result = {"email": email, "status": "error", "error": "Invalid response"}
                            result.setdefault("email", email)
                            results.append(result)
                        except Exception as e:
                            results.append({"email": email, "status": "error", "error": str(e)})

        # ── Summary ───────────────────────────────────────────
        processing_time = round(time.time() - start_time, 2)
        summary         = build_summary(results)

        return JsonResponse({
            "success":                  True,
            "total":                    len(results),
            "processing_time_seconds":  processing_time,
            "cache_hits":               len(cache_hits),
            "freshly_validated":        len(emails_to_validate),
            "threads_used":             min(MAX_WORKERS, max(len(emails_to_validate), 1)),
            "average_time_per_email":   round(processing_time / len(results), 3) if results else 0,
            "summary":                  summary,
            "results":                  results,
        })

    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


# ─────────────────────────────────────────────────────────────
# BATCH EMAIL API — STREAMING (NDJSON)
#   Streams one JSON object per line, AS SOON AS each email's
#   result is ready — instead of waiting for the whole batch.
#
#   Response format: newline-delimited JSON ("NDJSON")
#     {"type": "meta", ...}                  <- sent first
#     {"type": "result", "result": {...}}    <- one per email
#     {"type": "result", "result": {...}}
#     ...
#     {"type": "summary", ...}               <- sent last
#
#   Client reads the stream line-by-line (fetch + ReadableStream,
#   or `requests.iter_lines()` on the Python side) and can render
#   each row the moment it arrives.
# ─────────────────────────────────────────────────────────────
def _stream_batch_results(emails):
    """
    Generator that yields NDJSON lines as results become available.
    Cache hits are yielded immediately (near-zero cost). Uncached
    emails are validated in a thread pool and yielded as each
    future completes, in completion order (not input order).
    """
    start_time = time.time()

    # ── Batch cache lookup (single Redis round trip) ─────────
    cached_map = get_many_email_results(emails)

    cache_hits          = []
    emails_to_validate  = []

    for email in emails:
        cached = cached_map.get(email)
        if cached is not None:
            cache_hits.append(cached)
        else:
            emails_to_validate.append(email)

    total = len(emails)

    # ── Meta line: sent first so the client knows what to expect ──
    yield json.dumps({
        "type":               "meta",
        "total":              total,
        "cache_hits":         len(cache_hits),
        "to_validate":        len(emails_to_validate),
    }) + "\n"

    results = []

    # ── Yield cache hits immediately, one line each ───────────
    for r in cache_hits:
        results.append(r)
        yield json.dumps({"type": "result", "result": r}) + "\n"

    # ── Validate remaining emails, streaming each as it finishes ──
    if emails_to_validate:
        workers = min(MAX_WORKERS, len(emails_to_validate))

        if len(emails_to_validate) < 5:
            # Tiny batch: sequential is simpler and avoids thread
            # overhead; still streamed one line at a time.
            for email in emails_to_validate:
                r = safe_validate(email)
                results.append(r)
                yield json.dumps({"type": "result", "result": r}) + "\n"
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_map = {
                    executor.submit(safe_validate, email): email
                    for email in emails_to_validate
                }
                # as_completed yields futures in the order they FINISH,
                # not the order they were submitted — this is exactly
                # what gives you "first email done -> first line out".
                for future in as_completed(future_map, timeout=None):
                    email = future_map[future]
                    try:
                        result = future.result(timeout=10)
                        if not isinstance(result, dict):
                            result = {"email": email, "status": "error", "error": "Invalid response"}
                        result.setdefault("email", email)
                    except Exception as e:
                        result = {"email": email, "status": "error", "error": str(e)}

                    results.append(result)
                    yield json.dumps({"type": "result", "result": result}) + "\n"

    # ── Final summary line ────────────────────────────────────
    processing_time = round(time.time() - start_time, 2)
    summary = build_summary(results)

    yield json.dumps({
        "type":                     "summary",
        "success":                  True,
        "total":                    len(results),
        "processing_time_seconds":  processing_time,
        "cache_hits":               len(cache_hits),
        "freshly_validated":        len(emails_to_validate),
        "average_time_per_email":   round(processing_time / len(results), 3) if results else 0,
        "summary":                  summary,
    }) + "\n"


@csrf_exempt
def batch_validate_stream_api(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required"}, status=405)

    emails = []

    try:
        # ── Parse input (same logic as non-streaming endpoint) ──
        if "file" in request.FILES:
            file    = request.FILES["file"]
            decoded = file.read().decode("utf-8", errors="ignore").splitlines()
            reader  = csv.reader(decoded)
            for row in reader:
                if row:
                    e = row[0].strip().lower()
                    if "@" in e:
                        emails.append(e)
        else:
            data, error = parse_json_body(request)
            if error:
                return error
            emails = data.get("emails", [])

        cleaned = []
        for e in emails:
            if isinstance(e, str):
                e = e.strip().lower()
                if "@" in e:
                    cleaned.append(e)
        emails = list(set(cleaned))

        if not emails:
            return JsonResponse({"success": False, "error": "No valid emails found"}, status=400)

        if len(emails) > BATCH_MAX_EMAILS:
            return JsonResponse(
                {"success": False, "error": f"Maximum {BATCH_MAX_EMAILS} emails allowed"},
                status=400,
            )

    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)

    response = StreamingHttpResponse(
        _stream_batch_results(emails),
        content_type="application/x-ndjson",
    )
    # Prevent buffering by proxies (nginx) so lines actually
    # reach the client as they're yielded, not all at once at the end.
    response["Cache-Control"]      = "no-cache"
    response["X-Accel-Buffering"]  = "no"
    return response


# ─────────────────────────────────────────────────────────────
# CACHE STATS API  (GET /api/cache-stats/)
# ─────────────────────────────────────────────────────────────
@csrf_exempt
def cache_stats_api(request):
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "GET required"}, status=405)
    return JsonResponse({"success": True, "cache": cache_stats()})