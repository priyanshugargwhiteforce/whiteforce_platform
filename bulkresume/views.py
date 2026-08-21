import logging
import uuid

from django.core.exceptions import ValidationError
from notifications.authentication import ApiKeyAuthentication
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Resume
from .serializers import ResumeDetailSerializer, ResumeListSerializer
from .tasks import process_resume_task
from .throttles import BulkUploadThrottle

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

        serializer = ResumeListSerializer(resumes, many=True)
        return Response({"batch_id": batch_id, "resumes": serializer.data}, status=status.HTTP_200_OK)


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