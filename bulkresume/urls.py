from django.urls import path

from .views import (
    BatchStatusView,
    BulkResumeUploadView,
    JDMatchBatchStatusView,
    JDResumeMatchGeminiView,
    JDResumeMatchView,
    ResumeDetailView,
    SingleResumeParseView,
)

urlpatterns = [
    path('bulk-upload/', BulkResumeUploadView.as_view(), name='bulk-resume-upload'),
    path('parse/', SingleResumeParseView.as_view(), name='single-resume-parse'),
    path('batch/<str:batch_id>/status/', BatchStatusView.as_view(), name='batch-status'),
    path('match/', JDResumeMatchView.as_view(), name='jd-resume-match'),
    path('match/<str:batch_id>/status/', JDMatchBatchStatusView.as_view(), name='jd-resume-match-status'),
    path('match-gemini/', JDResumeMatchGeminiView.as_view(), name='jd-resume-match-gemini'),
    path('match-gemini/<str:batch_id>/status/', JDMatchBatchStatusView.as_view(), name='jd-resume-match-gemini-status'),
    path('<int:resume_id>/', ResumeDetailView.as_view(), name='resume-detail'),
]
