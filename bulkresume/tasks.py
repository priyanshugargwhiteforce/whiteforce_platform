import logging

from celery import shared_task
from django.conf import settings
from django.db import transaction

from .services.match_pipeline import process_resume_match
from .services.pipeline import process_resume

logger = logging.getLogger('bulkresume')

# No settings.py edit required for this one — override by adding
# MAX_CONCURRENT_RESUME_PARSES = <n> to settings.py if you want to, 20 is
# the default otherwise.
MAX_CONCURRENT_RESUME_PARSES = getattr(settings, 'MAX_CONCURRENT_RESUME_PARSES', 20)


@shared_task(
    bind=True,
    max_retries=2,
    retry_backoff=True,       # 1st retry ~2s, 2nd ~4s, instead of a flat 10s every time
    retry_backoff_max=60,     # cap the backoff so it never waits absurdly long
    retry_jitter=True,        # randomizes the backoff slightly, so N resumes that all
                               # hit a Groq rate-limit in the same second don't all
                               # retry at the exact same instant again (thundering herd)
)
def process_resume_task(self, resume_id: int):
    try:
        process_resume(resume_id)
    except ValueError:
        return
    except Exception as exc:
        raise self.retry(exc=exc)


@shared_task(
    bind=True,
    max_retries=2,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
)
def match_resume_task(self, resume_match_id: int):
    try:
        process_resume_match(resume_match_id)
    except Exception as exc:
        raise self.retry(exc=exc)


def claim_and_dispatch_pending_resumes() -> str:
    """
    Claims up to MAX_CONCURRENT_RESUME_PARSES pending resumes (oldest
    first), atomically flips them to 'queued' so nothing else can
    double-claim them, and dispatches one process_resume_task per resume.

    Shared by two callers:
      - dispatch_pending_resumes (below) — the Celery Beat periodic task,
        i.e. the actual "cron job" running inside your existing Celery
        infrastructure.
      - management/commands/process_pending_resumes.py — kept around for
        manual/local runs (e.g. testing without Beat running locally).
    """
    from .models import Resume  # local import avoids a circular import at module load time

    in_flight = Resume.objects.filter(status__in=['queued', 'processing']).count()
    slots = MAX_CONCURRENT_RESUME_PARSES - in_flight

    if slots <= 0:
        msg = f"{in_flight} resume(s) already in flight (limit {MAX_CONCURRENT_RESUME_PARSES}) — nothing claimed."
        logger.info(msg)
        return msg

    with transaction.atomic():
        claimed_ids = list(
            Resume.objects.select_for_update(skip_locked=True)
            .filter(status='pending')
            .order_by('uploaded_at')
            .values_list('id', flat=True)[:slots]
        )
        if claimed_ids:
            Resume.objects.filter(id__in=claimed_ids).update(status='queued')

    for resume_id in claimed_ids:
        process_resume_task.delay(resume_id)

    msg = f"Claimed and queued {len(claimed_ids)} resume(s) ({in_flight} already in flight, {slots} slot(s) available)."
    logger.info(msg)
    return msg


@shared_task
def dispatch_pending_resumes():
    """
    Celery Beat periodic task — this IS the cron job, running entirely
    inside your existing Celery worker/beat process. Scheduled via
    CELERY_BEAT_SCHEDULE in settings.py:

        CELERY_BEAT_SCHEDULE = {
            'dispatch-pending-resumes': {
                'task': 'bulkresume.tasks.dispatch_pending_resumes',
                'schedule': 30.0,  # every 30 seconds
            },
        }

    And start your worker with -B so it runs its own beat scheduler:

        celery -A core worker -l info -Q celery,resume_parsing -B
    """
    return claim_and_dispatch_pending_resumes()