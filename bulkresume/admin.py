from django.contrib import admin

from .models import JobDescription, ParsedProfile, Resume, ResumeMatch


@admin.register(Resume)
class ResumeAdmin(admin.ModelAdmin):
    list_display = ('id', 'file', 'status', 'file_type', 'batch_id', 'uploaded_at')
    list_filter = ('status', 'file_type')
    search_fields = ('batch_id',)


@admin.register(ParsedProfile)
class ParsedProfileAdmin(admin.ModelAdmin):
    list_display = ('id', 'resume', 'name', 'email', 'needs_review', 'extraction_method')
    list_filter = ('needs_review', 'extraction_method')
    search_fields = ('name', 'email', 'phone')


@admin.register(JobDescription)
class JobDescriptionAdmin(admin.ModelAdmin):
    list_display = ('id', 'batch_id', 'source_format', 'created_at')
    list_filter = ('source_format',)
    search_fields = ('batch_id',)


@admin.register(ResumeMatch)
class ResumeMatchAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'resume', 'jd', 'status', 'source', 'candidate_name',
        'match_percent', 'match_method', 'recommendation',
    )
    list_filter = ('status', 'source', 'match_method', 'recommendation')
    search_fields = ('candidate_name', 'phone')
