import dns.resolver

COMMON_PROVIDERS = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "yahoo.com",
    "zoho.com",
    "zoho.in"
}

SELECTORS = [
    "default",
    "selector1",
    "selector2",
    "google",
    "k1",
    "dkim",
    "mail",
    "smtp"
]


def check_dkim(domain):

    # Major providers always use DKIM internally
    if domain.lower() in COMMON_PROVIDERS:
        return True

    for selector in SELECTORS:
        try:

            records = dns.resolver.resolve(
                f"{selector}._domainkey.{domain}",
                "TXT"
            )

            for record in records:
                txt = str(record)

                if "DKIM1" in txt or "k=rsa" in txt:
                    return True

        except (
            dns.resolver.NXDOMAIN,
            dns.resolver.NoAnswer,
            dns.resolver.NoNameservers,
            dns.exception.Timeout
        ):
            continue

        except Exception:
            continue

    return False