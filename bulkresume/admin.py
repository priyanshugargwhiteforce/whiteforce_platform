from django.contrib import admin

from .models import ParsedProfile, Resume


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