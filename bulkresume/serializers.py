from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers

from .models import JobDescription, ParsedProfile, Resume, ResumeMatch


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
    parse_score = serializers.SerializerMethodField()

    class Meta:
        model = Resume
        fields = ['id', 'file', 'status', 'file_type', 'uploaded_at', 'error_message', 'parse_score', 'data']

    def get_parse_score(self, obj):
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


# ── JD <-> Resume matching ───────────────────────────────────────────────────

class JobDescriptionSerializer(serializers.ModelSerializer):
    # raw_text intentionally excluded — it's the full extracted JD text
    # (a wall of text), not useful in a match-results response. 'parsed'
    # already carries the structured fields the matching actually used.
    class Meta:
        model = JobDescription
        fields = ['id', 'batch_id', 'source_format', 'parsed', 'created_at']


class ResumeMatchSerializer(serializers.ModelSerializer):
    """
    Flat, front-loaded shape: overall_match_percent (0-100) and
    recommendation sit at the top level so they're the first thing you see
    per candidate. Supporting detail (matched/missing skills, field-by-field
    notes, summaries) is grouped under "analysis" instead of being spread
    across a dozen sibling keys. The full raw candidate_profile blob is
    intentionally left out — fetch /api/resumes/<resume_id>/ if you need
    the complete parsed profile for a candidate.
    """
    resume_id = serializers.IntegerField(source='resume.id', read_only=True)
    file_name = serializers.SerializerMethodField()
    overall_match_percent = serializers.SerializerMethodField()
    analysis = serializers.SerializerMethodField()

    class Meta:
        model = ResumeMatch
        fields = [
            'resume_id', 'file_name', 'status', 'candidate_name', 'phone', 'source',
            'overall_match_percent', 'recommendation', 'analysis', 'error_message',
        ]

    def get_file_name(self, obj):
        return obj.resume.file.name.rsplit('/', 1)[-1] if obj.resume.file else ""

    def get_overall_match_percent(self, obj):
        # Always 0-100. None only for resumes still pending/processing/failed.
        return round(obj.match_percent, 1) if obj.match_percent is not None else None

    def get_analysis(self, obj):
        return {
            "matched_skills": obj.matched_skills,
            "missing_skills": obj.missing_skills,
            "matched_qualifications": obj.matched_qualifications,
            "missing_qualifications": obj.missing_qualifications,
            "experience_match": obj.experience_match,
            "strengths_summary": obj.strengths_summary,
            "gaps_summary": obj.gaps_summary,
            "field_breakdown": obj.field_breakdown,
            "meta": {
                "extraction_method": obj.extraction_method,
                "parse_score": obj.parse_score,
                "match_method": obj.match_method,
            },
        }