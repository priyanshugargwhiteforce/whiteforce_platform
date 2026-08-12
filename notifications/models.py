from django.db import models


DOMAIN_CHOICES = (
    ('jobs', 'White Force Jobs'),
    ('attendance', 'White Force Attendance App'),
)


class Notification(models.Model):
    """
    One row = one piece of notification content created via the
    'create' API. It is sent (possibly more than once, to different
    token batches) via the 'send' API.
    """

    domain = models.CharField(max_length=20, choices=DOMAIN_CHOICES, db_index=True)

    title = models.CharField(max_length=255)
    body = models.TextField()
    regarding = models.CharField(
        max_length=255, blank=True, null=True,
        help_text="Short subject/category, e.g. 'Interview Update' or 'Leave Approved'."
    )

    # Either an uploaded file (served from /media/) or a ready-made external URL.
    image = models.ImageField(upload_to='notification_images/', blank=True, null=True)
    image_url = models.URLField(blank=True, null=True)

    # Auto-generated HTML rendering of the content above. Filled in by the view
    # after save() so that it can build an absolute URL for the image.
    html_content = models.TextField(blank=True, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)

    is_sent = models.BooleanField(default=False)
    sent_at = models.DateTimeField(blank=True, null=True)
    success_count = models.PositiveIntegerField(default=0)
    failure_count = models.PositiveIntegerField(default=0)

    # ── Real-time tag tracking ──────────────────────────────────────────
    # The WS "tags" this notification was last broadcast to (via
    # BroadcastNotificationView). Needed so that a later edit/delete
    # knows WHICH rooms to re-push the update/removal event to, without
    # requiring the caller to remember and re-supply the original tags.
    last_broadcast_tags = models.JSONField(default=list, blank=True)

    # ── Soft delete ──────────────────────────────────────────────────────
    # "Delete" here means: stop showing this in the live/real-time feed
    # (clients remove it from their list on the `notification_deleted`
    # WS event) while keeping the row + delivery logs for audit/history
    # (e.g. "oops, this went to the wrong tag" — you still want to know
    # what was sent, to whom, and when).
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(blank=True, null=True)

    # ── Edit tracking ─────────────────────────────────────────────────
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.domain}] {self.title}"

    def get_image_url(self, request=None):
        """Returns a usable image URL: prefers an external image_url,
        falls back to the uploaded file's absolute URL."""
        if self.image_url:
            return self.image_url
        if self.image:
            try:
                url = self.image.url
            except ValueError:
                return None
            if request is not None:
                return request.build_absolute_uri(url)
            return url
        return None

    # Domain-level display config: name, accent colour, icon (inline SVG path)
    DOMAIN_META = {
        'jobs': {
            'app_name':    'WhiteForce Jobs',
            'accent':      '#1a1a2e',
            'light_bg':    '#f0f0f5',
            'icon_svg': (
                '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" '
                'viewBox="0 0 24 24" fill="none" stroke="#ffffff" stroke-width="2" '
                'stroke-linecap="round" stroke-linejoin="round">'
                '<rect x="2" y="7" width="20" height="14" rx="2"/>'
                '<path d="M16 7V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2"/>'
                '</svg>'
            ),
        },
        'attendance': {
            'app_name':    'WhiteForce Attendance',
            'accent':      '#0f6e56',
            'light_bg':    '#e8f5f1',
            'icon_svg': (
                '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" '
                'viewBox="0 0 24 24" fill="none" stroke="#ffffff" stroke-width="2" '
                'stroke-linecap="round" stroke-linejoin="round">'
                '<circle cx="12" cy="12" r="10"/>'
                '<polyline points="12 6 12 12 16 14"/>'
                '</svg>'
            ),
        },
    }

    def build_html_content(self, request=None):
        """
        Renders a branded, self-contained HTML notification card.

        The card is designed to be dropped directly into a mobile WebView
        or an admin detail screen.  It adapts its colours and icon to the
        domain ('jobs' vs 'attendance') automatically.

        Layout
        ──────
        ┌─────────────────────────────────────┐
        │ [icon] WhiteForce <Domain>   • now  │  ← header bar (accent colour)
        ├─────────────────────────────────────┤
        │ [optional image]                    │
        │ REGARDING: …                        │
        │ Title                               │
        │ Body text …                         │
        │                                     │
        │ [Primary CTA]  [Dismiss]            │  ← action buttons
        └─────────────────────────────────────┘
        """
        meta      = self.DOMAIN_META.get(self.domain, self.DOMAIN_META['jobs'])
        app_name  = meta['app_name']
        accent    = meta['accent']
        light_bg  = meta['light_bg']
        icon_svg  = meta['icon_svg']

        image_url = self.get_image_url(request)
        body_html = self.body.replace('\n', '<br>')

        # ── optional blocks ────────────────────────────────────────────────
        image_block = (
            f'<img src="{image_url}" alt="{self.title}" '
            f'style="width:100%;max-height:180px;object-fit:cover;'
            f'border-radius:8px;margin-bottom:14px;display:block;">'
        ) if image_url else ''

        regarding_block = (
            f'<p style="font-size:11px;font-weight:600;color:{accent};'
            f'text-transform:uppercase;letter-spacing:0.6px;margin:0 0 6px 0;">'
            f'Regarding: {self.regarding}</p>'
        ) if self.regarding else ''

        # action label mirrors the domain context
        action_label = 'View Details' if self.domain == 'jobs' else 'View Record'

        return (
            # ── outer wrapper ──────────────────────────────────────────────
            '<div style="font-family:Arial,Helvetica,sans-serif;max-width:480px;'
            'border:1px solid #e0e0e0;border-radius:14px;overflow:hidden;'
            'box-shadow:0 2px 12px rgba(0,0,0,0.08);">'

            # ── header bar ─────────────────────────────────────────────────
            f'<div style="background:{accent};padding:12px 16px;'
            f'display:flex;align-items:center;justify-content:space-between;">'
            f'<div style="display:flex;align-items:center;gap:8px;">'
            f'<div style="width:28px;height:28px;border-radius:8px;'
            f'background:rgba(255,255,255,0.15);display:flex;align-items:center;'
            f'justify-content:center;">{icon_svg}</div>'
            f'<span style="color:#ffffff;font-size:13px;font-weight:600;'
            f'letter-spacing:0.3px;">{app_name}</span>'
            f'</div>'
            f'<span style="color:rgba(255,255,255,0.7);font-size:11px;">just now</span>'
            f'</div>'

            # ── body area ──────────────────────────────────────────────────
            f'<div style="background:#ffffff;padding:16px;">'
            f'{image_block}'
            f'{regarding_block}'
            f'<h3 style="margin:0 0 8px 0;font-size:16px;font-weight:700;'
            f'color:#1a1a1a;line-height:1.3;">{self.title}</h3>'
            f'<p style="color:#555555;font-size:14px;line-height:1.6;margin:0 0 16px 0;">'
            f'{body_html}</p>'

            # ── action buttons ─────────────────────────────────────────────
            f'<div style="display:flex;gap:10px;">'
            f'<a href="#" style="flex:1;text-align:center;background:{accent};'
            f'color:#ffffff;padding:10px 0;border-radius:8px;'
            f'text-decoration:none;font-size:13px;font-weight:600;">'
            f'{action_label}</a>'
            f'<a href="#" style="flex:1;text-align:center;background:{light_bg};'
            f'color:{accent};padding:10px 0;border-radius:8px;'
            f'text-decoration:none;font-size:13px;font-weight:600;">'
            f'Dismiss</a>'
            f'</div>'

            # ── footer stamp ───────────────────────────────────────────────
            f'<p style="margin:12px 0 0 0;font-size:11px;color:#aaaaaa;'
            f'text-align:right;">Powered by WhiteForce</p>'
            f'</div>'   # close body area
            f'</div>'   # close outer wrapper
        )


class NotificationDeliveryLog(models.Model):
    """One row per FCM token per send attempt -- useful for spotting
    dead/expired tokens your colleague's admin console should prune."""

    STATUS_CHOICES = (
        ('success', 'Success'),
        ('failed', 'Failed'),
    )

    notification = models.ForeignKey(
        Notification, on_delete=models.CASCADE, related_name='logs'
    )
    fcm_token = models.CharField(max_length=255)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    error_message = models.TextField(blank=True, null=True)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-sent_at']

    def __str__(self):
        return f"{self.notification_id} -> {self.fcm_token[:16]}... [{self.status}]"