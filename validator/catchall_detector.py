import dns.resolver
import smtplib
import uuid


def is_catchall(domain):

    try:

        mx_records = dns.resolver.resolve(domain, "MX")

        mx_host = str(
            sorted(mx_records, key=lambda x: x.preference)[0].exchange
        )

        fake_mailbox = f"{uuid.uuid4()}@{domain}"

        server = smtplib.SMTP(timeout=15)

        server.connect(mx_host)

        server.helo("example.com")

        server.mail("validator@example.com")

        code, _ = server.rcpt(fake_mailbox)

        server.quit()

        return code == 250

    except:
        return False