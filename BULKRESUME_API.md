# BulkResume API — routes, payloads and how each one works

App: `bulkresume` · Mounted at: `/api/resumes/` (see `core/urls.py`) · Framework: Django REST Framework + Celery

This document covers every route the `bulkresume` app exposes. Field names, limits and status codes were taken from the code (`bulkresume/views.py`, `serializers.py`, `models.py`, `services/*`), not from a running server. Example JSON uses made-up data and shows the shape, not real values.

---

## 1. Quick reference

| # | Method & path | Purpose | Sync / async | Parser used |
|---|---|---|---|---|
| 1 | `POST /api/resumes/bulk-upload/` | Upload many resumes to be parsed in the background | async (Celery) | Groq |
| 2 | `GET  /api/resumes/batch/<batch_id>/status/` | Progress and parsed results for a bulk batch | sync | — |
| 3 | `POST /api/resumes/parse/` | Parse ONE resume and return the data in the same response | sync | Gemini |
| 4 | `GET  /api/resumes/<resume_id>/` | Full parsed profile of one resume | sync | — |
| 5 | `POST /api/resumes/match/` | Match up to 15 resumes against one JD | async (Celery) | Groq parse, Gemini match |
| 6 | `POST /api/resumes/match-gemini/` | Same as 5, but Gemini also parses the resumes | async (Celery) | Gemini parse, Gemini match |
| 7 | `GET  /api/resumes/match/<batch_id>/status/` | Results of a match batch | sync | — |
| 8 | `GET  /api/resumes/match-gemini/<batch_id>/status/` | Same view as 7 (alias for the Gemini route) | sync | — |

Routes 7 and 8 are the same view. Either URL works for a batch created by either match route.

---

## 2. Common rules

### Authentication
Header: `X-API-KEY: <value of API_SECRET_KEY in .env>`

- If the header is sent and is wrong → **401** `Invalid or missing API key.`
- If `API_SECRET_KEY` is not configured on the server → **401** `Server misconfiguration: API key not configured.`
- If the header is **not sent at all**, these views currently still respond, because their permission class is `AllowAny` and the authenticator returns "no credentials". Send the key on every call anyway. If you want the key to be mandatory, that needs a permission change.

### Request format
`multipart/form-data` (the default parsers are `MultiPartParser` and `FormParser`). Files go in form fields; there is no JSON upload.

### File rules (all upload routes)
- Extensions: `.pdf`, `.doc`, `.docx`
- Empty files are rejected
- Max size: **10 MB** per file
- The server also identifies the file by content, not just extension: PDF (native text or scanned), image, DOCX, DOC. A file whose real type is unsupported fails at processing time (e.g. `Unsupported file type: application/zip`).

### Rate limits (per route)
| Route | Limit |
|---|---|
| bulk upload | 10/minute |
| single parse | 20/minute |
| JD match (routes 5 and 6) | 5/minute |
| status / detail GETs | none configured for these routes |

Exceeded → **429**.

### Resume `status` values
`pending` → `queued` → `processing` → `done` | `failed` | `duplicate`

- `pending`: saved, waiting for the dispatcher
- `queued`: claimed by the dispatcher, task sent to Celery
- `processing`: a worker is on it
- `done` / `failed`
- `duplicate`: bulk route only — the candidate's email/phone already exists in the candidate database, so parsing was skipped

### `extraction_method` values
| Value | Meaning |
|---|---|
| `llm` | Groq parsed it (bulk route) |
| `llm_ocr_deep_dive` | Groq parsed OCR text from the deep-dive pass |
| `llm_gemini` | Gemini parsed it (route 6) |
| `llm_gemini_fallback` | Groq failed, Gemini parsed it (route 5) |
| `llm_gemini_ocr_deep_dive` | Gemini parsed OCR text |
| `regex_fallback` | LLM failed; only email, phone and links were recovered — treat as `needs_review` |

---

## 3. Bulk upload flow

### 3.1 `POST /api/resumes/bulk-upload/`

**Request** — `multipart/form-data`

| Field | Type | Required | Notes |
|---|---|---|---|
| `files` | file, repeated | yes | 1 to **2000** files per request in the view, but Django's `DATA_UPLOAD_MAX_NUMBER_FILES` is set to **1500** in `core/settings.py`, so more than 1500 files in one request is rejected by Django first |

Example:
```bash
curl -X POST http://localhost:8000/api/resumes/bulk-upload/ \
  -H "X-API-KEY: <key>" \
  -F "files=@resume1.pdf" -F "files=@resume2.docx"
```

**Responses**

`202 Accepted`
```json
{
  "batch_id": "5f9a7706-c0f1-4a6b-809d-b7455ae878c9",
  "count": 2,
  "resume_ids": [433, 434],
  "status": "queued",
  "message": "2 resume(s) queued. They'll be parsed in the background (a limited number at a time) — poll the batch-status endpoint to watch progress."
}
```

`400` — nothing attached / more than 2000 files:
```json
{ "error": "No files provided. Attach at least one file under the 'files' field." }
```
`400` — a file failed validation (nothing is saved for the whole request):
```json
{
  "error": "One or more files failed validation",
  "details": [
    { "file": "notes.txt", "reason": "Unsupported file type '.txt'" },
    { "file": "big.pdf",   "reason": "File exceeds 10MB limit" }
  ]
}
```
`500` — could not save files.

**How it works**
1. All files are validated first, then a `batch_id` (UUID) is generated and one `Resume` row per file is created with `status=pending`.
2. The request does **not** process anything itself. It nudges the dispatcher (`kick_dispatch_if_pending`) and returns immediately.
3. The dispatcher (`tasks.claim_and_dispatch_pending_resumes`) claims the oldest `pending` resumes, sets them `queued`, and sends one `process_resume_task` per resume to Celery. It never lets more than **20** be `queued`/`processing` at once (`MAX_CONCURRENT_RESUME_PARSES`). A Postgres advisory lock stops parallel dispatchers from exceeding that cap.
4. The dispatcher runs again: on upload, every time a resume finishes (to refill the freed slot), and every 30 s from Celery Beat as a safety net.

### 3.2 What a worker does per resume (`services/pipeline.process_resume`)
1. Hash the file, detect its type, extract text (native PDF text, Tesseract OCR for scans/images, python-docx, LibreOffice for `.doc`), clean whitespace.
2. **Duplicate check:** regex-find email/phone in the text and call the White Force `check-candidate-exists` API (`DUPLICATE_CHECK_API_URL`, 8 s timeout). If the candidate exists → status `duplicate`, error message `Resume already parsed`, **no LLM call**. If the check API fails, it fails open and parsing continues.
3. **Groq extraction** (`services/llm_extractor.py`, model from `GROQ_MODEL`, default `openai/gpt-oss-20b`, JSON-schema output, `reasoning_effort=low`). Up to 7500 characters of the resume are sent.
4. **Clean-up of the model output:**
   - `json_coerce` repairs near-miss keys, `null`s and wrong types instead of failing the parse.
   - `grounding` clears any education/job/project value that does not appear in the resume text, and drops made-up entries.
   - If the resume states no total experience, `total_experience` is computed in code from the written job durations.
5. **Parse score** (0–100): field coverage (65 %) + text quality (35 %).
6. **OCR deep-dive:** if the score is ≤ 55 (`OCR_DEEP_DIVE_SCORE_THRESHOLD`), the file is re-OCR'd with stronger preprocessing and re-parsed; the better-scoring result is kept. It is skipped when the low score came from an LLM failure on clean text (OCR would not help).
7. If the LLM call fails entirely, regex extraction is used and `needs_review` is set (`extraction_method: regex_fallback`).
8. Save/update `ParsedProfile`, set the resume `done`. On errors the Celery task retries twice with backoff, then the resume is `failed`.

### 3.3 `GET /api/resumes/batch/<batch_id>/status/`

`batch_id` must be a UUID (else `400 {"error": "Invalid batch_id format"}`); unknown batch → `404`.

**Response `200`**
```json
{
  "batch_id": "5f9a7706-…",
  "summary": {
    "total": 20,
    "llm_parsed": 9,
    "regex_fallback": 0,
    "ocr_deep_dive_used": 1,
    "average_parse_score": 92.4,
    "duplicate_skipped": 10,
    "failed": 1,
    "pending": 0
  },
  "resumes": [
    {
      "id": 434,
      "file": "/media/bulkresume/resumes/2026/09/30/resume1.pdf",
      "status": "done",
      "error_message": "",
      "parse_score": 95.45,
      "data": { "…": "see 'Parsed profile' below" }
    }
  ]
}
```
- `summary.pending` counts `pending`, `queued` and `processing`.
- `data` is `null` for resumes that have no profile (still processing, `failed`, or `duplicate`).
- Poll this until `pending` is 0.

---

## 4. Single parse

### 4.1 `POST /api/resumes/parse/`

**Request** — `multipart/form-data`

| Field | Type | Required | Notes |
|---|---|---|---|
| `file` | file | yes | exactly one file |

**Response `200`** — the resume and its parsed profile (same shape as route 4):
```json
{
  "id": 501,
  "file": "/media/bulkresume/resumes/2026/09/30/priya.pdf",
  "status": "done",
  "file_type": "pdf_native",
  "uploaded_at": "2026-09-30T07:29:55.958117Z",
  "error_message": "",
  "parse_score": 96.1,
  "data": { "…": "see 'Parsed profile' below" }
}
```
Errors: `400` (no file / bad type / empty / too large), `422` when parsing itself fails:
```json
{ "error": "Failed to parse resume", "detail": "<reason>", "resume_id": 501 }
```

**How it works:** fully synchronous, no Celery, and **no duplicate check** — every upload is parsed. Uses **Gemini** (`services/gemini_extractor.py`, model `GEMINI_MODEL`, default `gemini-3.5-flash-lite`), the same scoring and OCR deep-dive logic as bulk, and saves a `Resume` + `ParsedProfile` so it can be fetched again by id. If Gemini fails it falls back to regex fields.

### 4.2 `GET /api/resumes/<resume_id>/`
Integer id. Returns the same object as 4.1. `404` → `{"error": "Resume with id '…' not found"}`.

---

## 5. Parsed profile (`data`)

Returned inside routes 2, 3 and 4. All fields always exist on a profile.

| Field | Type | Notes |
|---|---|---|
| `name`, `email`, `phone` | string | |
| `gender`, `date_of_birth`, `marital_status` | string | as written |
| `father_name`, `mother_name` | string | |
| `known_languages` | string[] | spoken languages only |
| `candidate_address`, `pincode_postal_code` | string | |
| `linkedin_url` | string | |
| `other_urls` | string[] | |
| `total_experience` | string | stated on the resume, or computed from job durations (e.g. `"3 years"`) |
| `education` | object[] | `{degree, institution, year, percentage}` — `percentage` is a string as written (`"83.78 %"`, `"8.4 CGPA"`) or `null` |
| `experience` | object[] | `{title, company, duration}` (bulk route: no `description`) |
| `internships` | object[] | same shape as `experience` |
| `projects` | object[] or `null` | `{title, company, start_date, end_date, description, methodologies[]}`; **`null` when the resume has no projects** (bulk route) |
| `training` | object[] | `{name, provider, year}` |
| `skills`, `certifications`, `hobbies` | string[] | |
| `summary` | string | resume's own summary/objective, or a short generated one |
| `needs_review` | boolean | `true` if only regex extraction was possible |
| `extraction_method` | string | see section 2 |
| `parse_score` | number | 0–100 |
| `ocr_deep_dive_used` | boolean | |

Rules the parsers follow: extract only what is written (no guessing), copy text as written, one item per education row / job / project, unknown text → `""`, unknown lists → `[]`.

Differences between routes:
- **Bulk (Groq)**: `experience`/`internships` items have no `description`; `projects` is `null` when none.
- **Single parse (Gemini)**: `projects` is `[]` when none. It shares the same `experience` shape (its prompt no longer asks for a description).
- Records saved before these changes keep any old `description` keys; nothing rewrites stored data.

---

## 6. JD ↔ resume matching

Two routes create a match batch; they differ only in how each **resume** is parsed.

| | `POST /api/resumes/match/` | `POST /api/resumes/match-gemini/` |
|---|---|---|
| Resume parser | Groq (bulk route), Gemini if Groq fails | Gemini, own schema that keeps job/project descriptions |
| JD parser | Gemini | Gemini |
| Match | Gemini + verification | Gemini + verification |
| Celery task | `match_resume_task` | `match_resume_gemini_task` |

### 6.1 Request (both routes) — `multipart/form-data`

| Field | Type | Required | Notes |
|---|---|---|---|
| `resumes` | file, repeated | yes | 1 to **15** files |
| `jd_file` | file | one of three | JD as pdf/doc/docx |
| `jd_text` | string | one of three | JD pasted as plain text |
| `jd_json` | string (JSON) | one of three | either already in the JD shape (see below) or a free-form blob such as `{"description": "…"}` |

**Exactly one** of `jd_file` / `jd_text` / `jd_json` must be sent, otherwise `400 {"error": "Provide exactly one of: jd_file, jd_text, jd_json."}`.

JD shape accepted directly in `jd_json` (no LLM call when any of these keys is present):
```json
{
  "job_title": "Software Developer",
  "must_have_skills": ["Python", "SQL"],
  "good_to_have_skills": ["Docker"],
  "min_experience_years": "0",
  "max_experience_years": "2",
  "qualifications": ["Bachelor's degree in Computer Science or related field"],
  "responsibilities": ["Design, develop, test and maintain applications"],
  "location": "Mumbai",
  "employment_type": "Full-time",
  "other_requirements": []
}
```

Example:
```bash
curl -X POST http://localhost:8000/api/resumes/match-gemini/ \
  -H "X-API-KEY: <key>" \
  -F "jd_text=Elevator Service Technician - Mumbai. 2-4 years experience ..." \
  -F "resumes=@a.pdf" -F "resumes=@b.pdf"
```

### 6.2 Response

`202 Accepted`
```json
{
  "batch_id": "0ec3ddb1-34cb-4206-a477-29556ac2d29f",
  "jd_id": 10,
  "jd_preview": { "job_title": "…", "must_have_skills": ["…"], "…": "…" },
  "count": 3
}
```
`207 Multi-Status` — files saved but some could not be queued; adds `"warning"` and `"failed_to_queue": [match ids]`.

`400` — invalid `jd_json`, bad JD file type, no resumes, more than 15 resumes, or an invalid resume file (`details` lists them).
`500` — the JD could not be processed, or files could not be saved.

The JD is structured **before** any resume rows are created, so a JD failure leaves nothing behind. `jd_preview` is the structured JD used for matching: check it for empty lists (a warning is logged when the JD came back essentially empty).

### 6.3 What happens per resume (async)
1. Resume is set `processing`; file hashed and classified. **No duplicate check** — every resume is parsed fresh.
2. **Parse:**
   - Route 5: Groq bulk parser (`services/bulk_route_parse.py`). If Groq falls back to regex, Gemini is tried once before accepting the poor result.
   - Route 6: Gemini (`services/gemini_resume_parser.py`, schema `services/match_schemas.py`).
   - Both apply the same clean-up (coercion, "not in resume" clearing, computed total experience), score the parse, and OCR-retry when the score is low and the LLM did not fail.
3. **Match** (`services/matcher.py` + `services/gemini_match.py`): Gemini compares a trimmed candidate profile (skills, experience, internships, projects, education, certifications, training, address, summary — no contact details or personal data) with the JD, one row per JD requirement, at temperature 0.
4. **Verification** (`services/match_verify.py`) before scoring:
   - every JD requirement ends up with exactly one row, in JD order (missing rows are filled by text search);
   - a claimed match whose quoted evidence is not in the resume is downgraded to 0;
   - a skill marked missing but present word-for-word in the resume is upgraded;
   - the experience row is computed from numbers (candidate total years vs JD min/max), not by the model.
5. **Score** (`matcher.reconcile_match_result`): overall = weighted average of the row `match_percent`s. Weights: must-have skill 3, experience 2, good-to-have skill 1, qualification 1, location 1.
   `recommendation`: **≥ 75 Strong Match**, **≥ 40 Partial Match**, otherwise **Weak Match**.
6. If the Gemini match fails or returns no rows, the **deterministic fallback** is used (skill overlap only: 80 % must-have, 20 % good-to-have); `match_method` is then `deterministic_fallback` and `field_breakdown` is `[]`.
7. Results are saved on `ResumeMatch`; the resume is `done`. Errors retry twice, then `failed`.

### 6.4 `GET /api/resumes/match/<batch_id>/status/` (also `/match-gemini/<batch_id>/status/`)

`400` invalid UUID · `404` unknown batch.

**Response `200`**
```json
{
  "batch_id": "0ec3ddb1-…",
  "job_description": {
    "id": 10,
    "batch_id": "0ec3ddb1-…",
    "source_format": "file",
    "parsed": { "job_title": "…", "must_have_skills": ["…"], "…": "…" },
    "created_at": "2026-09-30T07:29:55.958117Z"
  },
  "summary": {
    "total": 3,
    "done": 3,
    "failed": 0,
    "pending": 0,
    "from_db_profile": 0,
    "freshly_parsed": 3,
    "average_match_percent": 16.67,
    "highest_match_percent": 38.5,
    "lowest_match_percent": 3,
    "requirement_rollup": [
      { "requirement": "Python", "type": "must_have", "matched_by": 1,
        "total_candidates": 3, "match_rate_percent": 33.3 }
    ]
  },
  "matches": [
    {
      "resume_id": 432,
      "file_name": "candidate.pdf",
      "status": "done",
      "candidate_name": "Jane Doe",
      "phone": "+91 …",
      "source": "parsed",
      "overall_match_percent": 38.5,
      "recommendation": "Weak Match",
      "analysis": {
        "matched_skills": ["Python", "SQL"],
        "missing_skills": ["Docker"],
        "matched_qualifications": ["Bachelor's degree in Computer Science …"],
        "missing_qualifications": [],
        "experience_match": "Meets requirement: 2 years vs 0+ required",
        "strengths_summary": "…",
        "gaps_summary": "…",
        "field_breakdown": [
          {
            "field": "must_have_skill",
            "jd_requirement": "Python",
            "candidate_value": "Python",
            "matched": true,
            "match_percent": 100.0,
            "note": "Explicitly listed in skills."
          }
        ],
        "meta": { "extraction_method": "llm_gemini", "parse_score": 95.45, "match_method": "llm" }
      },
      "error_message": ""
    }
  ]
}
```

Notes:
- `matches` is ordered by `match_percent` descending in the database query. On PostgreSQL, rows with no score yet (`overall_match_percent: null`, still running) sort **before** the scored ones, so sort on the client if you need finished results first. Unfinished rows have empty analysis lists.
- `summary.pending` counts `pending` and `processing`.
- `requirement_rollup` lists each JD must-have / good-to-have skill with how many finished candidates matched it — it shows which requirement is the bottleneck for the whole batch.
- `source` is `parsed` (the DB-profile shortcut is not used).
- The full parsed profile is **not** in this response; the candidate profile is stored on the match record only.

`field_breakdown[]` details:
| Key | Meaning |
|---|---|
| `field` | always one of `must_have_skill`, `good_to_have_skill`, `qualification`, `experience`, `location` |
| `jd_requirement` | the requirement as written in `job_description.parsed` (experience shown as `2-4 years`, `2+ years`, `up to 4 years`) |
| `candidate_value` | evidence quoted from the candidate's profile / resume; `""` when none |
| `matched` | `true` only when `match_percent` ≥ 60 |
| `match_percent` | 0–100 |
| `note` | short reason. Fixed phrases can appear: `Evidence not found in resume`, `No evidence in resume`, `Found verbatim in resume`, `Checked by text search`, `Address matches job location`, `Location not matched`, `Candidate experience not stated` |

`strengths_summary` / `gaps_summary` are normally short prose from the model. When verification corrected any row they are rebuilt from the lists (`Matches: A, B.` / `Missing: X, Y.`), so allow for long text.

---

## 7. Infrastructure and how to run it

### Processes
```
Django API server      python manage.py runserver     (or gunicorn/uwsgi)
Celery worker + Beat   celery -A core worker -l info -P threads -c 10 -Q celery,resume_parsing -B
```
- `-B` runs Beat, which fires the dispatcher every 30 s (`CELERY_BEAT_SCHEDULE` in `core/settings.py`).
- The work is mostly waiting on the duplicate-check API and the LLMs, so a threaded pool (or `-c 5` on the Linux prefork pool) lets several resumes run at once. A single-process worker (`solo`) runs them strictly one after another.
- Redis is the broker and result backend. Files go to `MEDIA_ROOT`. Database: PostgreSQL.
- **Restart the worker after any code change** (only the API server auto-reloads).

### Environment / settings that matter
| Setting | Purpose | Default |
|---|---|---|
| `API_SECRET_KEY` | value expected in `X-API-KEY` | none |
| `GROQ_API_KEY_*` (several) | Groq keys, rotated round-robin | — |
| `GROQ_MODEL` | Groq model | `openai/gpt-oss-20b` |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Gemini | model `gemini-3.5-flash-lite` |
| `DUPLICATE_CHECK_API_URL` | candidate-exists API; empty = no duplicate check | empty |
| `MAX_CONCURRENT_RESUME_PARSES` | bulk in-flight cap | 20 |
| `OCR_DEEP_DIVE_SCORE_THRESHOLD` | deep-dive when score ≤ this | 55 |
| `BULK_RESUME_MAX_CHARS` | resume text sent to Groq | 7500 |
| `GROQ_REASONING_EFFORT` | `low` / `medium` / `high` | `low` |
| `GROQ_MIN_SECONDS_BETWEEN_CALLS` | pause before each Groq call | 0.3 |
| `TESSERACT_CMD`, `POPPLER_PATH`, `SOFFICE_PATH` | OCR / PDF / `.doc` tools | OS-specific defaults |

### Data model
- `Resume` — the uploaded file, its status, `batch_id`, extracted text, error message.
- `ParsedProfile` — one per parsed resume (route 1 and 3); the fields in section 5.
- `JobDescription` — one per match request; `parsed` holds the structured JD.
- `ResumeMatch` — one per resume in a match request; holds the candidate profile and all match results.

### Logs
`logs/platform.log` records every stage, including `Claimed N resume(s)…`, duplicate-check results, `grounding: cleared … (not found in resume text)`, and per-call Gemini/Groq token usage.

---

## 8. Which route should I use?

- **Upload many resumes to store them** → `bulk-upload` then poll `batch/<id>/status`.
- **Show one parsed resume immediately** → `parse`.
- **Rank resumes for a job** → `match-gemini` (keeps job/project descriptions for better matching) or `match`; poll the match status route.
- **Fetch one stored profile again** → `<resume_id>`.
