from django.urls import path

from .views import BatchStatusView, BulkResumeUploadView, ResumeDetailView

urlpatterns = [
    path('bulk-upload/', BulkResumeUploadView.as_view(), name='bulk-resume-upload'),
    path('batch/<str:batch_id>/status/', BatchStatusView.as_view(), name='batch-status'),
    path('<int:resume_id>/', ResumeDetailView.as_view(), name='resume-detail'),
]