from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),

    # Instance management — no tag needed
    path('python/api/', include('tallyapp.instance_urls')),

    # Default instance data endpoints (no tag → uses DEFAULT_TALLY_TAG in views.py)
    path('python/api/', include('tallyapp.urls')),

    # Tag-specific instance data endpoints
    path('python/<str:tally_tag>/api/', include('tallyapp.urls')),

    path('api/', include('validator.urls')),

    path('api/', include('notifications.urls')),

    path('api/resumes/', include('bulkresume.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)