from django.db import models


class Resume(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('queued', 'Queued'),
        ('processing', 'Processing'),
        ('done', 'Done'),
        ('failed', 'Failed'),
        ('duplicate', 'Duplicate'),
    ]

    file = models.FileField(upload_to='bulkresume/resumes/%Y/%m/%d/')
    file_type = models.CharField(max_length=30, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    raw_text = models.TextField(blank=True)
    file_hash = models.CharField(max_length=64, db_index=True, blank=True)
    batch_id = models.CharField(max_length=50, db_index=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    error_message = models.TextField(blank=True)
    is_duplicate = models.BooleanField(default=False)

    def __str__(self):
        return f"Resume#{self.id} [{self.status}]"


class ParsedProfile(models.Model):
    resume = models.OneToOneField(Resume, on_delete=models.CASCADE, related_name='profile')
    name = models.CharField(max_length=255, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=30, blank=True, null=True)
    gender = models.CharField(max_length=20, blank=True, null=True)
    date_of_birth = models.CharField(max_length=20, blank=True, null=True)
    marital_status = models.CharField(max_length=20, blank=True, null=True)
    father_name = models.CharField(max_length=255, blank=True, null=True)
    mother_name = models.CharField(max_length=255, blank=True, null=True)
    candidate_address = models.TextField(blank=True, null=True)
    pincode_postal_code = models.CharField(max_length=20, blank=True, null=True)
    hobbies = models.JSONField(default=list, blank=True, null=True)
    training = models.JSONField(default=list, blank=True, null=True)
    known_languages = models.JSONField(default=list, blank=True)
    linkedin_url = models.URLField(blank=True, null=True)
    other_urls = models.JSONField(default=list, blank=True)
    education = models.JSONField(default=list, blank=True)
    experience = models.JSONField(default=list, blank=True)
    skills = models.JSONField(default=list, blank=True)
    certifications = models.JSONField(default=list, blank=True)
    internships = models.JSONField(default=list, blank=True)
    summary = models.TextField(blank=True)
    needs_review = models.BooleanField(default=False)
    extraction_method = models.CharField(max_length=30, blank=True, null=True)
    # ── Parse-quality scoring / OCR deep-dive ──────────────────────────
    parse_score = models.FloatField(blank=True, null=True)
    ocr_deep_dive_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Profile({self.name or 'unknown'})"


# ── JD <-> Resume matching (new) ────────────────────────────────────────────

class JobDescription(models.Model):
    SOURCE_CHOICES = [
        ('file', 'File'),
        ('json', 'JSON'),
        ('text', 'Text'),
    ]

    batch_id = models.CharField(max_length=50, db_index=True)
    source_format = models.CharField(max_length=10, choices=SOURCE_CHOICES)
    file = models.FileField(upload_to='bulkresume/jds/%Y/%m/%d/', blank=True, null=True)
    raw_text = models.TextField(blank=True)
    # Structured JobDescriptionExtraction fields (see services/schemas.py),
    # stored as JSON so both the API response and the matcher can use the
    # exact same structured shape without re-deriving it.
    parsed = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"JD#{self.id} [{self.source_format}] batch={self.batch_id}"


class ResumeMatch(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ]
    SOURCE_CHOICES = [
        ('db_profile', 'DB Profile'),
        ('parsed', 'Freshly Parsed'),
    ]

    jd = models.ForeignKey(JobDescription, related_name='matches', on_delete=models.CASCADE)
    resume = models.OneToOneField(Resume, related_name='match', on_delete=models.CASCADE)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    error_message = models.TextField(blank=True)

    # ── How the candidate profile was obtained ──────────────────────────
    phone = models.CharField(max_length=30, blank=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, blank=True)
    extraction_method = models.CharField(max_length=30, blank=True, null=True)
    parse_score = models.FloatField(blank=True, null=True)
    candidate_name = models.CharField(max_length=255, blank=True)
    candidate_profile = models.JSONField(default=dict, blank=True)

    # ── Match result ─────────────────────────────────────────────────────
    match_method = models.CharField(max_length=30, blank=True)  # 'llm' or 'deterministic_fallback'
    match_percent = models.FloatField(blank=True, null=True)
    matched_skills = models.JSONField(default=list, blank=True)
    missing_skills = models.JSONField(default=list, blank=True)
    matched_qualifications = models.JSONField(default=list, blank=True)
    missing_qualifications = models.JSONField(default=list, blank=True)
    experience_match = models.CharField(max_length=255, blank=True)
    field_breakdown = models.JSONField(default=list, blank=True)
    strengths_summary = models.TextField(blank=True)
    gaps_summary = models.TextField(blank=True)
    recommendation = models.CharField(max_length=50, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Match(resume#{self.resume_id} vs jd#{self.jd_id}) {self.match_percent}%"
