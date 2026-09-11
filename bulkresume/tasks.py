from celery import shared_task

from .services.match_pipeline import process_resume_match
from .services.pipeline import process_resume


@shared_task(bind=True, max_retries=2, default_retry_delay=10)
def process_resume_task(self, resume_id: int):
    try:
        process_resume(resume_id)
    except ValueError:
        return
    except Exception as exc:
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=10)
def match_resume_task(self, resume_match_id: int):
    try:
        process_resume_match(resume_match_id)
    except Exception as exc:
        raise self.retry(exc=exc)
