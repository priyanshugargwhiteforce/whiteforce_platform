from django.urls import path
from .views import (
    validate_email_api,
    batch_validate_api,
    batch_validate_stream_api,
    cache_stats_api,
)

urlpatterns = [
    path("validate-email/",         validate_email_api,         name="validate_email_api"),
    path("batch-validate/",         batch_validate_api,         name="batch_validate_api"),
    path("batch-validate-stream/",  batch_validate_stream_api,  name="batch_validate_stream_api"),
    path("cache-stats/",            cache_stats_api,            name="cache_stats_api"),
]