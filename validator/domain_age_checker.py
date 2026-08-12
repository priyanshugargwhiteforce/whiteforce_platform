import whois
from datetime import datetime


KNOWN_DOMAINS = {
    "gmail.com": 20,
    "googlemail.com": 20,
    "outlook.com": 20,
    "hotmail.com": 20,
    "live.com": 20,
    "yahoo.com": 20,
    "zoho.com": 20,
    "zoho.in": 20,
}


def get_domain_age_score(domain):

    domain = domain.lower()

    # Fast path for major providers
    if domain in KNOWN_DOMAINS:
        return KNOWN_DOMAINS[domain]

    try:

        info = whois.whois(domain)

        created = info.creation_date

        if not created:
            return 0

        if isinstance(created, list):
            created = created[0]

        # Remove timezone if present
        if hasattr(created, "tzinfo") and created.tzinfo:
            created = created.replace(tzinfo=None)

        years = (datetime.now() - created).days / 365.25

        if years >= 10:
            return 20

        elif years >= 5:
            return 15

        elif years >= 1:
            return 10

        else:
            return 5

    except Exception as e:
        print("WHOIS Error:", e)
        return 0