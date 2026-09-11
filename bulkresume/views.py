import hashlib
import logging
import uuid

from django.core.exceptions import ValidationError
from notifications.authentication import ApiKeyAuthentication
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import JobDescription, ParsedProfile, Resume, ResumeMatch
from .serializers import (
    JobDescriptionSerializer,
    ResumeDetailSerializer,
    ResumeListSerializer,
    ResumeMatchSerializer,
)
from .services.file_classifier import classify_file
from .services.jd_extractor import (
    JDExtractionError,
    extract_jd_from_file,
    extract_jd_from_json,
    extract_jd_from_text,
)
from .services.single_parse import build_profile_defaults, parse_single_resume
from .tasks import match_resume_task, process_resume_task
from .throttles import BulkUploadThrottle, JDMatchThrottle, SingleResumeParseThrottle

logger = logging.getLogger(__name__)

# Tune these to your actual constraints
MAX_FILES_PER_BATCH = 50
ALLOWED_EXTENSIONS = {'.pdf', '.doc', '.docx'}
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

# Resume<->JD matching does 1-2 LLM calls per resume (parse, if not already
# in the DB, + match), so keep the per-request batch smaller than the plain
# bulk-upload one above. Bump if/when Groq capacity allows.
MAX_MATCH_RESUMES = 10


class BulkResumeUploadView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [AllowAny]
    throttle_classes = [BulkUploadThrottle]
    throttle_scope = 'bulk_resume_upload'

    def post(self, request):
        files = request.FILES.getlist('files')

        if not files:
            return Response(
                {"error": "No files provided. Attach at least one file under the 'files' field."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if len(files) > MAX_FILES_PER_BATCH:
            return Response(
                {"error": f"Too many files in one request. Max allowed is {MAX_FILES_PER_BATCH}, got {len(files)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate every file before creating any DB rows, so a bad file
        # in the batch doesn't leave a partially-created batch behind.
        invalid_files = []
        for f in files:
            ext = ('.' + f.name.rsplit('.', 1)[-1].lower()) if '.' in f.name else ''
            if ext not in ALLOWED_EXTENSIONS:
                invalid_files.append({"file": f.name, "reason": f"Unsupported file type '{ext}'"})
            elif f.size == 0:
                invalid_files.append({"file": f.name, "reason": "File is empty"})
            elif f.size > MAX_FILE_SIZE_BYTES:
                invalid_files.append({"file": f.name, "reason": f"File exceeds {MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB limit"})

        if invalid_files:
            return Response(
                {"error": "One or more files failed validation", "details": invalid_files},
                status=status.HTTP_400_BAD_REQUEST,
            )

        batch_id = str(uuid.uuid4())
        resume_ids = []

        try:
            for f in files:
                resume = Resume.objects.create(file=f, batch_id=batch_id)
                resume_ids.append(resume.id)
        except ValidationError as e:
            logger.warning("Validation error creating resumes for batch %s: %s", batch_id, e)
            return Response(
                {"error": "Invalid file data", "details": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception("Failed to create Resume records for batch %s", batch_id)
            return Response(
                {"error": "Could not save uploaded files. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # DB rows exist at this point — queueing failures shouldn't look like
        # total failure to the client, since the resumes were saved.
        queue_failures = []
        for resume_id in resume_ids:
            try:
                process_resume_task.delay(resume_id)
            except Exception:
                logger.exception("Failed to queue processing task for resume %s (batch %s)", resume_id, batch_id)
                queue_failures.append(resume_id)

        if queue_failures:
            return Response(
                {
                    "batch_id": batch_id,
                    "count": len(resume_ids),
                    "resume_ids": resume_ids,
                    "warning": "Some resumes were saved but could not be queued for processing",
                    "failed_to_queue": queue_failures,
                },
                status=status.HTTP_207_MULTI_STATUS,
            )

        return Response(
            {"batch_id": batch_id, "count": len(resume_ids), "resume_ids": resume_ids},
            status=status.HTTP_202_ACCEPTED,
        )


class SingleResumeParseView(APIView):
    """
    Synchronous single-file parse-and-return endpoint.

    Unlike BulkResumeUploadView (above), this:
      - takes exactly ONE file under the 'file' field
      - does NOT check the duplicate-candidate DB (no check_duplicate_in_db
        call) — every upload gets parsed, every time
      - does NOT queue a Celery task — parsing happens inline, and the full
        extracted profile is returned in this same response
      - still saves Resume + ParsedProfile rows (so it shows up in admin /
        GET resume-detail like any other parse, and benefits from the same
        parse_score / OCR deep-dive logic as the bulk pipeline)

    BulkResumeUploadView, BatchStatusView, and the async Celery flow in
    services/pipeline.py are completely unaffected by this endpoint.
    """
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [AllowAny]
    throttle_classes = [SingleResumeParseThrottle]
    throttle_scope = 'single_resume_parse'

    def post(self, request):
        f = request.FILES.get('file')

        if not f:
            return Response(
                {"error": "No file provided. Attach exactly one file under the 'file' field."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ext = ('.' + f.name.rsplit('.', 1)[-1].lower()) if '.' in f.name else ''
        if ext not in ALLOWED_EXTENSIONS:
            return Response({"error": f"Unsupported file type '{ext}'"}, status=status.HTTP_400_BAD_REQUEST)
        if f.size == 0:
            return Response({"error": "File is empty"}, status=status.HTTP_400_BAD_REQUEST)
        if f.size > MAX_FILE_SIZE_BYTES:
            return Response(
                {"error": f"File exceeds {MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB limit"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            resume = Resume.objects.create(file=f)
        except ValidationError as e:
            logger.warning("Validation error creating resume for single parse: %s", e)
            return Response({"error": "Invalid file data", "details": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("Failed to save uploaded file for single-resume parse")
            return Response(
                {"error": "Could not save uploaded file. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        resume.status = 'processing'
        resume.save(update_fields=['status'])

        try:
            file_path = resume.file.path

            with open(file_path, 'rb') as fh:
                resume.file_hash = hashlib.sha256(fh.read()).hexdigest()

            file_type = classify_file(file_path)
            resume.file_type = file_type

            result = parse_single_resume(file_path, file_type, log_prefix=f"Resume#{resume.id} (single): ")

            resume.raw_text = result["raw_text"]
            resume.save(update_fields=['file_type', 'raw_text', 'file_hash'])

            ParsedProfile.objects.update_or_create(
                resume=resume,
                defaults=build_profile_defaults(
                    result["extracted"], result["needs_review"], result["extraction_method"],
                    result["score"], result["ocr_deep_dive_used"],
                ),
            )

            resume.status = 'done'
            resume.save(update_fields=['status'])

        except Exception as exc:
            resume.status = 'failed'
            resume.error_message = str(exc)
            resume.save(update_fields=['status', 'error_message'])
            logger.exception(f"Resume#{resume.id}: single-resume parse failed")
            return Response(
                {"error": "Failed to parse resume", "detail": str(exc), "resume_id": resume.id},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        serializer = ResumeDetailSerializer(resume)
        return Response(serializer.data, status=status.HTTP_200_OK)


class BatchStatusView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [AllowAny]

    def get(self, request, batch_id):
        try:
            uuid.UUID(str(batch_id))
        except (ValueError, TypeError):
            return Response(
                {"error": "Invalid batch_id format"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        resumes = Resume.objects.filter(batch_id=batch_id).select_related('profile')

        if not resumes.exists():
            return Response(
                {"error": f"No resumes found for batch_id '{batch_id}'"},
                status=status.HTTP_404_NOT_FOUND,
            )

        total = resumes.count()
        duplicate_count = resumes.filter(status='duplicate').count()
        done_count = resumes.filter(status='done').count()
        failed_count = resumes.filter(status='failed').count()
        pending_count = resumes.filter(status__in=['pending', 'processing']).count()

        # Of the ones marked 'done', how many actually hit the LLM vs.
        # fell back to plain regex extraction (e.g. LLM call failed).
        llm_parsed_count = resumes.filter(status='done', profile__extraction_method='llm').count()
        regex_fallback_count = resumes.filter(status='done', profile__extraction_method='regex_fallback').count()
        ocr_deep_dive_count = resumes.filter(status='done', profile__ocr_deep_dive_used=True).count()

        # Average parse_score across resumes that actually have one (i.e.
        # status='done' — pending/processing/failed/duplicate have no
        # ParsedProfile row yet, so they're naturally excluded here).
        scores = list(
            resumes.filter(status='done', profile__parse_score__isnull=False)
            .values_list('profile__parse_score', flat=True)
        )
        average_parse_score = round(sum(scores) / len(scores), 2) if scores else None

        summary = {
            "total": total,
            "llm_parsed": llm_parsed_count,
            "regex_fallback": regex_fallback_count,
            "ocr_deep_dive_used": ocr_deep_dive_count,
            "average_parse_score": average_parse_score,
            "duplicate_skipped": duplicate_count,
            "failed": failed_count,
            "pending": pending_count,
        }

        serializer = ResumeListSerializer(resumes, many=True)
        return Response(
            {"batch_id": batch_id, "summary": summary, "resumes": serializer.data},
            status=status.HTTP_200_OK,
        )


class ResumeDetailView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [AllowAny]

    def get(self, request, resume_id):
        try:
            resume = Resume.objects.select_related('profile').get(id=resume_id)
        except (Resume.DoesNotExist, ValueError, ValidationError):
            # ValueError/ValidationError covers a malformed id (e.g. non-UUID/non-int)
            # depending on your primary key type, so it doesn't 500 on bad input.
            return Response(
                {"error": f"Resume with id '{resume_id}' not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception:
            logger.exception("Unexpected error fetching resume %s", resume_id)
            return Response(
                {"error": "Something went wrong while fetching this resume"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        serializer = ResumeDetailSerializer(resume)
        return Response(serializer.data, status=status.HTTP_200_OK)


# ── JD <-> Resume matching (new) ────────────────────────────────────────────

class JDResumeMatchView(APIView):
    """
    POST /api/resumes/match/

    Body (multipart/form-data):
      resumes  : 1-10 resume files, repeated under the 'resumes' field
      jd_file  : the JD as a file (pdf/doc/docx)   -- OR --
      jd_text  : the JD as plain pasted text        -- OR --
      jd_json  : the JD as a JSON string — either already shaped like
                 JobDescriptionExtraction (job_title, must_have_skills, ...)
                 or a free-form blob (e.g. {"description": "..."})

    Exactly one of jd_file / jd_text / jd_json must be provided.

    Async, same shape as BulkResumeUploadView: creates the JobDescription +
    Resume + ResumeMatch rows, queues one Celery task per resume (phone
    regex -> DB profile lookup or fresh parse -> JD match), and returns a
    batch_id immediately. Poll JDMatchBatchStatusView for per-resume results
    plus the batch-wide summary.
    """
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [AllowAny]
    throttle_classes = [JDMatchThrottle]
    throttle_scope = 'jd_resume_match'

    def post(self, request):
        files = request.FILES.getlist('resumes')
        jd_file = request.FILES.get('jd_file')
        jd_text = request.data.get('jd_text')
        jd_json = request.data.get('jd_json')

        provided = [v for v in (jd_file, jd_text, jd_json) if v]
        if len(provided) != 1:
            return Response(
                {"error": "Provide exactly one of: jd_file, jd_text, jd_json."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not files:
            return Response(
                {"error": "No resumes provided. Attach 1-10 files under the 'resumes' field."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(files) > MAX_MATCH_RESUMES:
            return Response(
                {"error": f"Too many resumes in one request. Max allowed is {MAX_MATCH_RESUMES}, got {len(files)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        invalid_files = []
        for f in files:
            ext = ('.' + f.name.rsplit('.', 1)[-1].lower()) if '.' in f.name else ''
            if ext not in ALLOWED_EXTENSIONS:
                invalid_files.append({"file": f.name, "reason": f"Unsupported file type '{ext}'"})
            elif f.size == 0:
                invalid_files.append({"file": f.name, "reason": "File is empty"})
            elif f.size > MAX_FILE_SIZE_BYTES:
                invalid_files.append({"file": f.name, "reason": f"File exceeds {MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB limit"})
        if invalid_files:
            return Response(
                {"error": "One or more resume files failed validation", "details": invalid_files},
                status=status.HTTP_400_BAD_REQUEST,
            )

        batch_id = str(uuid.uuid4())

        # ── Build the JD row first — if JD extraction itself fails, don't
        # create any Resume/ResumeMatch rows at all. ───────────────────────
        try:
            if jd_file:
                ext = ('.' + jd_file.name.rsplit('.', 1)[-1].lower()) if '.' in jd_file.name else ''
                if ext not in ALLOWED_EXTENSIONS:
                    return Response({"error": f"Unsupported JD file type '{ext}'"}, status=status.HTTP_400_BAD_REQUEST)
                jd_row = JobDescription.objects.create(batch_id=batch_id, source_format='file', file=jd_file)
                raw_text, parsed = extract_jd_from_file(jd_row.file.path)
                jd_row.raw_text = raw_text
                jd_row.parsed = parsed
                jd_row.save(update_fields=['raw_text', 'parsed'])
            elif jd_text:
                raw_text, parsed = extract_jd_from_text(jd_text)
                jd_row = JobDescription.objects.create(
                    batch_id=batch_id, source_format='text', raw_text=raw_text, parsed=parsed,
                )
            else:  # jd_json
                raw_text, parsed = extract_jd_from_json(jd_json)
                jd_row = JobDescription.objects.create(
                    batch_id=batch_id, source_format='json', raw_text=raw_text, parsed=parsed,
                )
        except JDExtractionError as e:
            return Response({"error": f"Invalid jd_json: {e}"}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception(f"Failed to extract JD for batch {batch_id}")
            return Response(
                {"error": "Could not process the job description. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        if not any(jd_row.parsed.get(k) for k in
                    ('must_have_skills', 'good_to_have_skills', 'qualifications', 'responsibilities', 'job_title')):
            logger.warning(f"JD#{jd_row.id} (batch {batch_id}): structured extraction came back essentially empty")

        # ── Resume + ResumeMatch rows ──────────────────────────────────────
        match_ids = []
        try:
            for f in files:
                resume = Resume.objects.create(file=f, batch_id=batch_id)
                match = ResumeMatch.objects.create(jd=jd_row, resume=resume, status='pending')
                match_ids.append(match.id)
        except ValidationError as e:
            logger.warning(f"Validation error creating match resumes for batch {batch_id}: {e}")
            return Response({"error": "Invalid file data", "details": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception(f"Failed to create Resume/ResumeMatch records for batch {batch_id}")
            return Response(
                {"error": "Could not save uploaded files. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        queue_failures = []
        for match_id in match_ids:
            try:
                match_resume_task.delay(match_id)
            except Exception:
                logger.exception(f"Failed to queue match task {match_id} (batch {batch_id})")
                queue_failures.append(match_id)

        response_data = {
            "batch_id": batch_id,
            "jd_id": jd_row.id,
            "jd_preview": jd_row.parsed,
            "count": len(match_ids),
        }
        if queue_failures:
            response_data["warning"] = "Some resumes were saved but could not be queued for matching"
            response_data["failed_to_queue"] = queue_failures
            return Response(response_data, status=status.HTTP_207_MULTI_STATUS)

        return Response(response_data, status=status.HTTP_202_ACCEPTED)


class JDMatchBatchStatusView(APIView):
    """
    GET /api/resumes/match/<batch_id>/status/

    Same polling shape as BatchStatusView, but for the JD-matching batch:
    per-resume match results (sorted best-match-first) plus a batch-wide
    summary — average/highest/lowest match percent, how many candidates
    came from the DB vs. a fresh parse, and a per-requirement rollup
    showing what fraction of candidates in this batch matched each
    must-have / good-to-have skill on the JD.
    """
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [AllowAny]

    def get(self, request, batch_id):
        try:
            uuid.UUID(str(batch_id))
        except (ValueError, TypeError):
            return Response({"error": "Invalid batch_id format"}, status=status.HTTP_400_BAD_REQUEST)

        matches = ResumeMatch.objects.filter(resume__batch_id=batch_id).select_related('resume', 'jd')
        if not matches.exists():
            return Response(
                {"error": f"No matches found for batch_id '{batch_id}'"},
                status=status.HTTP_404_NOT_FOUND,
            )

        jd_row = matches.first().jd
        total = matches.count()
        done = matches.filter(status='done')
        done_count = done.count()
        failed_count = matches.filter(status='failed').count()
        pending_count = matches.filter(status__in=['pending', 'processing']).count()
        db_profile_count = done.filter(source='db_profile').count()
        parsed_count = done.filter(source='parsed').count()

        scores = list(done.filter(match_percent__isnull=False).values_list('match_percent', flat=True))
        average_match_percent = round(sum(scores) / len(scores), 2) if scores else None
        highest_match_percent = max(scores) if scores else None
        lowest_match_percent = min(scores) if scores else None

        summary = {
            "total": total,
            "done": done_count,
            "failed": failed_count,
            "pending": pending_count,
            "from_db_profile": db_profile_count,
            "freshly_parsed": parsed_count,
            "average_match_percent": average_match_percent,
            "highest_match_percent": highest_match_percent,
            "lowest_match_percent": lowest_match_percent,
            "requirement_rollup": _build_requirement_rollup(jd_row.parsed, done),
        }

        serializer = ResumeMatchSerializer(matches.order_by('-match_percent'), many=True)
        return Response(
            {
                "batch_id": batch_id,
                "job_description": JobDescriptionSerializer(jd_row).data,
                "summary": summary,
                "matches": serializer.data,
            },
            status=status.HTTP_200_OK,
        )


def _build_requirement_rollup(jd_parsed: dict, done_matches) -> list:
    """For each must-have / good-to-have skill on the JD, what fraction of
    the (done) candidates in this batch matched it — e.g. "AWS" matched by
    6/10 candidates. Lets a recruiter see at a glance which requirements
    are the real bottleneck across the whole batch, not just per-resume."""
    all_reqs = (
        [(s, "must_have") for s in jd_parsed.get("must_have_skills", [])]
        + [(s, "good_to_have") for s in jd_parsed.get("good_to_have_skills", [])]
    )
    if not all_reqs:
        return []

    done_list = list(done_matches)
    total_done = len(done_list)
    rollup = []
    for skill, kind in all_reqs:
        skill_lower = skill.strip().lower()
        matched_count = sum(
            1 for m in done_list
            if any(skill_lower in s.lower() or s.lower() in skill_lower for s in (m.matched_skills or []))
        )
        rollup.append({
            "requirement": skill,
            "type": kind,
            "matched_by": matched_count,
            "total_candidates": total_done,
            "match_rate_percent": round(matched_count / total_done * 100, 1) if total_done else 0,
        })
    return rollup
