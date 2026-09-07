from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers

from .models import ParsedProfile, Resume


class ParsedProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = ParsedProfile
        fields = [
            'name', 'email', 'phone', 'gender', 'date_of_birth', 'marital_status', 'father_name', 'mother_name', 'known_languages', 'candidate_address', 'pincode_postal_code', 'hobbies', 'training', 'linkedin_url', 'other_urls',
            'education', 'experience', 'skills', 'certifications',
            'internships', 'summary', 'needs_review', 'extraction_method',
            'parse_score', 'ocr_deep_dive_used',
        ]


class ResumeDetailSerializer(serializers.ModelSerializer):
    data = ParsedProfileSerializer(source='profile', read_only=True)
    # Also surfaced at the top level (in addition to data.parse_score) so
    # it's visible without digging into the nested profile object — same
    # value either way, just easier to scan at a glance.
    parse_score = serializers.SerializerMethodField()

    class Meta:
        model = Resume
        fields = ['id', 'file', 'status', 'file_type', 'uploaded_at', 'error_message', 'parse_score', 'data']

    def get_parse_score(self, obj):
        # No ParsedProfile row yet for 'pending'/'processing'/'failed'/'duplicate'
        # resumes — return None instead of raising.
        try:
            return obj.profile.parse_score
        except ObjectDoesNotExist:
            return None


class ResumeListSerializer(serializers.ModelSerializer):
    data = ParsedProfileSerializer(source='profile', read_only=True)
    parse_score = serializers.SerializerMethodField()

    class Meta:
        model = Resume
        fields = ['id', 'file', 'status', 'error_message', 'parse_score', 'data']

    def get_parse_score(self, obj):
        try:
            return obj.profile.parse_score
        except ObjectDoesNotExist:
            return None