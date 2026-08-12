DISPOSABLE = {
    "tempmail.com",
    "mailinator.com",
    "10minutemail.com",
    "guerrillamail.com",
    "yopmail.com"
}


def detect_typo(domain):
    return domain in DISPOSABLE