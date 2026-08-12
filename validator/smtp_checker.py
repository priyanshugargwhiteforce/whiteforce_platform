import dns.resolver
import smtplib
import socket
import time
import random

# Global socket timeout
socket.setdefaulttimeout(5)


class SMTPVerifier:

    def __init__(
        self,
        timeout=3,
        retry=0,
        connect_timeout=2.5,
        max_mx_hosts=2,
    ):
        self.timeout = timeout
        self.retry = retry
        self.connect_timeout = connect_timeout
        self.max_mx_hosts = max_mx_hosts

    # -------------------------------------------------
    # MAIN SMTP VERIFY FUNCTION
    # -------------------------------------------------
    def smtp_verify(self, email, hard_deadline=6.0):
        """
        hard_deadline: absolute max wall-clock seconds this function
        will spend on ONE email, no matter how many MX hosts/retries
        are configured. This is what actually caps worst-case latency —
        individual timeouts (connect_timeout, timeout) only bound a
        single command, not the whole function.
        """
        verify_start = time.time()

        email = email.strip().lower()

        # ---------------------------------------------
        # BASIC EMAIL CHECK
        # ---------------------------------------------
        if "@" not in email:
            return {
                "smtp_valid": False,
                "smtp_status": "invalid_email_format"
            }

        domain = email.split("@")[-1]

        # ---------------------------------------------
        # SKIP SMTP FOR SOME PROVIDERS
        # (Optional Optimization)
        # # ---------------------------------------------
        # skip_domains = [
        #     "gmail.com",
        #     "googlemail.com"
        # ]

        # if domain in skip_domains:
        #     return {
        #         "smtp_valid": None,
        #         "smtp_status": "smtp_skipped_provider_policy"
        #     }

        # ---------------------------------------------
        # MX LOOKUP
        # ---------------------------------------------
        try:

            resolver = dns.resolver.Resolver()
            resolver.timeout = 2.5
            resolver.lifetime = 3.0

            mx_records = resolver.resolve(
                domain,
                "MX"
            )

            mx_records = sorted(
                mx_records,
                key=lambda x: x.preference
            )

            mx_hosts = [
                str(mx.exchange).rstrip(".")
                for mx in mx_records
            ]

        except dns.resolver.NXDOMAIN:

            return {
                "smtp_valid": False,
                "smtp_status": "domain_not_found"
            }

        except dns.resolver.NoAnswer:

            return {
                "smtp_valid": False,
                "smtp_status": "no_mx_records"
            }

        except dns.resolver.Timeout:

            return {
                "smtp_valid": None,
                "smtp_status": "dns_timeout"
            }

        except Exception as e:

            return {
                "smtp_valid": None,
                "smtp_status": "dns_failure",
                "error": str(e)
            }

        # -------------------------------------------------
        # TRY MULTIPLE MX SERVERS (capped, no artificial sleep)
        # -------------------------------------------------
        port_25_blocked = False

        for mx_host in mx_hosts[: self.max_mx_hosts]:

            # If we've already detected port 25 is blocked at the
            # network level, don't waste time retrying other MX hosts.
            if port_25_blocked:
                break

            # Hard deadline check — don't start a new MX attempt if
            # we're already close to the wall-clock budget for this email.
            if time.time() - verify_start > hard_deadline:
                return {
                    "smtp_valid": None,
                    "smtp_status": "smtp_deadline_exceeded",
                }

            # Retry loop
            for attempt in range(self.retry + 1):

                server = None

                try:

                    # -------------------------------------
                    # SMTP CONNECT
                    # -------------------------------------
                    server = smtplib.SMTP(timeout=self.connect_timeout)
                    server.connect(mx_host, 25)

                    # Force the same tight timeout on every
                    # subsequent command (ehlo/mail/rcpt), not just
                    # the initial connect — this is what was letting
                    # individual emails hang for 10-20+ seconds.
                    server.sock.settimeout(self.timeout)

                    # Use the real HELO hostname (your VPS's own
                    # domain) instead of the default local hostname —
                    # helps avoid greylisting by servers that check
                    # HELO against reverse DNS.
                    server.ehlo("astro-buddy.in")

                    # Sender must be a domain that actually resolves
                    # and has valid MX/SPF — using a fake non-existent
                    # domain here gets you blocked/greylisted by many
                    # providers (especially Outlook/Microsoft).
                    server.mail(
                        "verify@astro-buddy.in"
                    )

                    # RCPT CHECK
                    code, message = server.rcpt(email)

                    # Decode message
                    if isinstance(message, bytes):
                        smtp_message = message.decode(
                            errors="ignore"
                        )
                    else:
                        smtp_message = str(message)

                    smtp_message_lower = (
                        smtp_message.lower()
                    )

                    # Debug logs
                    print("=" * 60)
                    print("EMAIL:", email)
                    print("MX HOST:", mx_host)
                    print("SMTP CODE:", code)
                    print("SMTP MESSAGE:", smtp_message)
                    print("=" * 60)

                    # Close connection
                    try:
                        server.quit()
                    except Exception:
                        pass

                    # -------------------------------------
                    # VALID MAILBOX
                    # -------------------------------------
                    if code == 250:

                        return {
                            "smtp_valid": True,
                            "smtp_status": "mailbox_exists_likely",
                            "smtp_code": code,
                            "smtp_message": smtp_message,
                            "mx_server": mx_host
                        }

                    # -------------------------------------
                    # TEMPORARY FAILURE
                    # -------------------------------------
                    elif code in [421, 450, 451, 452]:

                        if attempt < self.retry:
                            continue

                        return {
                            "smtp_valid": None,
                            "smtp_status": "temporary_failure",
                            "smtp_code": code,
                            "smtp_message": smtp_message,
                            "mx_server": mx_host
                        }

                    # -------------------------------------
                    # MAILBOX NOT FOUND
                    # -------------------------------------
                    elif code in [550, 551, 553]:

                        block_keywords = [
                            "verification",
                            "policy",
                            "access denied",
                            "not authorized",
                            "anti-spam",
                            "security",
                            "blocked",
                            "disabled",
                            "rate limit",
                            "too many",
                            "denied"
                        ]

                        # Provider blocked validation
                        if any(
                            keyword in smtp_message_lower
                            for keyword in block_keywords
                        ):

                            return {
                                "smtp_valid": None,
                                "smtp_status": "verification_blocked",
                                "smtp_code": code,
                                "smtp_message": smtp_message,
                                "mx_server": mx_host
                            }

                        # Mailbox invalid
                        return {
                            "smtp_valid": False,
                            "smtp_status": "mailbox_does_not_exist",
                            "smtp_code": code,
                            "smtp_message": smtp_message,
                            "mx_server": mx_host
                        }

                    # -------------------------------------
                    # CATCH-ALL POSSIBILITY
                    # -------------------------------------
                    elif code == 252:

                        return {
                            "smtp_valid": None,
                            "smtp_status": "accept_all_possible",
                            "smtp_code": code,
                            "smtp_message": smtp_message,
                            "mx_server": mx_host
                        }

                    # -------------------------------------
                    # UNKNOWN RESPONSE
                    # -------------------------------------
                    else:

                        return {
                            "smtp_valid": None,
                            "smtp_status": f"unknown_response_{code}",
                            "smtp_code": code,
                            "smtp_message": smtp_message,
                            "mx_server": mx_host
                        }

                # -----------------------------------------
                # CONNECTION ERRORS
                # -----------------------------------------
                except smtplib.SMTPServerDisconnected:

                    continue

                except ConnectionRefusedError:

                    # Outbound port 25 actively refused — VPS/host
                    # is almost certainly blocking SMTP egress.
                    # Don't burn time retrying other MX hosts.
                    port_25_blocked = True
                    break

                except smtplib.SMTPConnectError:

                    continue

                except socket.timeout:

                    # Connect-level timeout on port 25 usually means
                    # the port is silently filtered by the host/VPS
                    # firewall rather than the mail server being slow.
                    port_25_blocked = True
                    break

                except OSError as e:
                    # Covers "Network is unreachable" / "No route to host"
                    # which also indicate port 25 egress is blocked.
                    if "unreachable" in str(e).lower() or "no route" in str(e).lower():
                        port_25_blocked = True
                        break
                    continue

                except dns.resolver.Timeout:

                    continue

                except Exception as e:

                    print("SMTP ERROR:", str(e))

                    continue

                finally:

                    try:
                        if server:
                            server.quit()
                    except Exception:
                        pass

        # -------------------------------------------------
        # FINAL FAILURE
        # -------------------------------------------------
        if port_25_blocked:
            return {
                "smtp_valid": None,
                "smtp_status": "smtp_port_blocked",
                "note": "Outbound port 25 appears blocked on this server/VPS. "
                        "Ask your host to open it, or route SMTP checks through "
                        "a provider/relay that allows port 25 egress."
            }

        return {
            "smtp_valid": None,
            "smtp_status": "smtp_verification_failed"
        }


# ---------------------------------------------------------
# SINGLETON INSTANCE
# ---------------------------------------------------------
verifier = SMTPVerifier(
    timeout=3,
    retry=0,
    connect_timeout=2.5,
    max_mx_hosts=2,
)


# ---------------------------------------------------------
# WRAPPER FUNCTION
# ---------------------------------------------------------
def smtp_verify(email):
    return verifier.smtp_verify(email)