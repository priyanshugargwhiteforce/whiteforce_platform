import re

import phonenumbers

EMAIL_RE = re.compile(r'[\w\.-]+@[\w\.-]+\.\w+')
LINKEDIN_RE = re.compile(r'(https?://)?(www\.)?linkedin\.com/in/[\w\-]+')
URL_RE = re.compile(r'https?://[^\s]+')


def regex_extract_basic_fields(text: str) -> dict:
    email_match = EMAIL_RE.search(text)
    linkedin_match = LINKEDIN_RE.search(text)
    urls = URL_RE.findall(text)

    phone = ""
    for match in phonenumbers.PhoneNumberMatcher(text, "IN"):
        phone = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164)
        break

    return {
        "name": "",
        "email": email_match.group() if email_match else "",
        "phone": phone,
        "linkedin_url": linkedin_match.group() if linkedin_match else "",
        "other_urls": [u for u in urls if 'linkedin' not in u],
        "education": [],
        "experience": [],
        "skills": [],
        "certifications": [],
        "internships": [],
        "profile_summary": "",
    }