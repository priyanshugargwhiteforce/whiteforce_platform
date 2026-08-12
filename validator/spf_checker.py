import dns.resolver

def check_spf(domain):
    try:
        records = dns.resolver.resolve(domain, "TXT")

        for record in records:
            txt = str(record)

            if "v=spf1" in txt:
                return True

        return False

    except:
        return False