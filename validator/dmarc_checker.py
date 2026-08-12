import dns.resolver

def check_dmarc(domain):
    try:

        dmarc_domain = f"_dmarc.{domain}"

        records = dns.resolver.resolve(
            dmarc_domain,
            "TXT"
        )

        for record in records:

            txt = str(record)

            if "v=DMARC1" in txt:
                return True

        return False

    except:
        return False