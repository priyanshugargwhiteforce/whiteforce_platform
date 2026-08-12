def reputation_score(domain):

    trusted = {
        "gmail.com": 20,
        "outlook.com": 20,
        "hotmail.com": 20,
        "live.com": 20,
        "zoho.com": 20,
        "zoho.in": 20,
        "yahoo.com": 18
    }

    if domain in trusted:
        return trusted[domain]

    return 10