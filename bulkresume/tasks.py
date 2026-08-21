from celery import shared_task
from .services.pipeline import process_resume


@shared_task(bind=True, max_retries=2, default_retry_delay=10)
def process_resume_task(self, resume_id: int):
    try:
        process_resume(resume_id)
    except ValueError:
        return
    except Exception as exc:
        raise self.retry(exc=exc)