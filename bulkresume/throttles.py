from notifications.throttles import _LoggedThrottle


class BulkUploadThrottle(_LoggedThrottle):
    scope = 'bulk_resume_upload'