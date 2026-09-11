from notifications.throttles import _LoggedThrottle


class BulkUploadThrottle(_LoggedThrottle):
    scope = 'bulk_resume_upload'


class SingleResumeParseThrottle(_LoggedThrottle):
    scope = 'single_resume_parse'


class JDMatchThrottle(_LoggedThrottle):
    scope = 'jd_resume_match'
