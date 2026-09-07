import hashlib
import logging
import uuid

from django.core.exceptions import ValidationError
from notifications.authentication import ApiKeyAuthentication
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ParsedProfile, Resume
from .serializers import ResumeDetailSerializer, ResumeListSerializer
from .services.file_classifier import classify_file
from .services.single_parse import build_profile_defaults, parse_single_resume
from .tasks import process_resume_task
from .throttles import BulkUploadThrottle, SingleResumeParseThrottle

logger = logging.getLogger(__name__)

# Tune these to your actual constraints
MAX_FILES_PER_BATCH = 50
ALLOWED_EXTENSIONS = {'.pdf', '.doc', '.docx'}
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB


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