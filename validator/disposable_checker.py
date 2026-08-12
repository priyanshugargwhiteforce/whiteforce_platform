DISPOSABLE = {
    "tempmail.com",
    "mailinator.com",
    "10minutemail.com",
    "guerrillamail.com",
    "yopmail.com"
}


def is_disposable(domain):
    return domain in DISPOSABLE