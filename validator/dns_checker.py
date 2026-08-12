import dns.resolver


def check_mx(domain):
    try:
        records = dns.resolver.resolve(
            domain,
            'MX'
        )

        return len(records) > 0

    except:
        return False