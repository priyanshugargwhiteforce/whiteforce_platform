"""
Manual/local entry point for the bulk-resume queue.

The actual "cron job" now runs as a Celery Beat periodic task —
bulkresume.tasks.dispatch_pending_resumes, scheduled via
CELERY_BEAT_SCHEDULE in settings.py and run inside your Celery worker
process (started with -B). See tasks.py for the full wiring notes.

This command still exists for convenience: it calls the exact same
claim-and-dispatch logic (bulkresume.tasks.claim_and_dispatch_pending_resumes)
on demand, which is handy when:
  - you're developing locally and don't have Beat running
  - you want to force an immediate claim without waiting for the next
    scheduled tick
  - you're debugging and want to see the claim result printed directly
    to your terminal instead of digging through Celery/worker logs

Usage:
    python manage.py process_pending_resumes
"""
from django.core.management.base import BaseCommand

from bulkresume.tasks import claim_and_dispatch_pending_resumes


class Command(BaseCommand):
    help = "Claims up to MAX_CONCURRENT_RESUME_PARSES pending resumes and dispatches them for parsing."

    def handle(self, *args, **options):
        result = claim_and_dispatch_pending_resumes()
        self.stdout.write(result)