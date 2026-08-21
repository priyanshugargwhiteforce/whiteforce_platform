from rest_framework import serializers

from .models import ParsedProfile, Resume


class ParsedProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = ParsedProfile
        fields = [
            'name', 'email', 'phone', 'linkedin_url', 'other_urls',
            'education', 'experience', 'skills', 'certifications',
            'internships', 'summary', 'needs_review', 'extraction_method',
        ]


class ResumeDetailSerializer(serializers.ModelSerializer):
    data = ParsedProfileSerializer(source='profile', read_only=True)

    class Meta:
        model = Resume
        fields = ['id', 'file', 'status', 'file_type', 'uploaded_at', 'error_message', 'data']


class ResumeListSerializer(serializers.ModelSerializer):
    data = ParsedProfileSerializer(source='profile', read_only=True)

    class Meta:
        model = Resume
        fields = ['id', 'file', 'status', 'error_message', 'data']