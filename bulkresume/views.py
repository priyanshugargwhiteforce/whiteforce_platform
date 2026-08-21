import uuid

from notifications.authentication import ApiKeyAuthentication
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Resume
from .serializers import ResumeDetailSerializer, ResumeListSerializer
from .tasks import process_resume_task
from .throttles import BulkUploadThrottle


class BulkResumeUploadView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [AllowAny]
    throttle_classes = [BulkUploadThrottle]
    throttle_scope = 'bulk_resume_upload'

    def post(self, request):
        files = request.FILES.getlist('files')
        if not files:
            return Response({"error": "No files provided"}, status=status.HTTP_400_BAD_REQUEST)

        batch_id = str(uuid.uuid4())
        resume_ids = []

        for f in files:
            resume = Resume.objects.create(file=f, batch_id=batch_id)
            resume_ids.append(resume.id)
            process_resume_task.delay(resume.id)

        return Response(
            {"batch_id": batch_id, "count": len(resume_ids), "resume_ids": resume_ids},
            status=status.HTTP_202_ACCEPTED,
        )


class BatchStatusView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [AllowAny]

    def get(self, request, batch_id):
        resumes = Resume.objects.filter(batch_id=batch_id).select_related('profile')
        serializer = ResumeListSerializer(resumes, many=True)
        return Response({"batch_id": batch_id, "resumes": serializer.data})


class ResumeDetailView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [AllowAny]

    def get(self, request, resume_id):
        try:
            resume = Resume.objects.select_related('profile').get(id=resume_id)
        except Resume.DoesNotExist:
            return Response({"error": "Resume not found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = ResumeDetailSerializer(resume)
        return Response(serializer.data, status=status.HTTP_200_OK)