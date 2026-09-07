from django.db import models


class Resume(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
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