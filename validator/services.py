import re

from .dns_checker        import check_mx
from .disposable_checker import is_disposable
from .typo_detector      import detect_typo
from .reputaion_checker  import reputation_score
from .smtp_checker       import smtp_verify
from .catchall_detector  import is_catchall
from .spf_checker        import check_spf
from .dkim_checker       import check_dkim
from .dmarc_checker      import check_dmarc
from .domain_age_checker import get_domain_age_score
from .role_account_checker import is_role_account
# from .abstract_checker   import abstract_verify
from .scoring_engine     import calculate_score
from .random_detector    import is_random_username

# ── Redis cache layer (gracefully disabled if Redis is down) ──
from .redis_cache import (
    get_cached_email_result, set_cached_email_result,
    get_cached_domain,       set_cached_domain,
    get_cached_smtp,         set_cached_smtp,
)


# ─────────────────────────────────────────────────────────────
# CACHED WRAPPERS FOR DOMAIN-LEVEL CHECKS
#   Same domain → identical DNS/SPF/DKIM/DMARC/reputation/age
#   These are cached per-domain, not per-email.
# ─────────────────────────────────────────────────────────────

def _cached_mx(domain):
    v = get_cached_domain(domain, 'mx')
    if v is None:
        v = check_mx(domain)
        set_cached_domain(domain, 'mx', v)
    return v

def _cached_spf(domain):
    v = get_cached_domain(domain, 'spf')
    if v is None:
        v = check_spf(domain)
        set_cached_domain(domain, 'spf', v)
    return v

def _cached_dkim(domain):
    v = get_cached_domain(domain, 'dkim')
    if v is None:
        v = check_dkim(domain)
        set_cached_domain(domain, 'dkim', v)
    return v

def _cached_dmarc(domain):
    v = get_cached_domain(domain, 'dmarc')
    if v is None:
        v = check_dmarc(domain)
        set_cached_domain(domain, 'dmarc', v)
    return v

def _cached_reputation(domain):
    v = get_cached_domain(domain, 'reputation')
    if v is None:
        v = reputation_score(domain)
        set_cached_domain(domain, 'reputation', v)
    return v

def _cached_domain_age(domain):
    v = get_cached_domain(domain, 'domain_age')
    if v is None:
        v = get_domain_age_score(domain)
        set_cached_domain(domain, 'domain_age', v)
    return v

def _cached_disposable(domain):
    v = get_cached_domain(domain, 'disposable')
    if v is None:
        v = is_disposable(domain)
        set_cached_domain(domain, 'disposable', v)
    return v

def _cached_typo(domain):
    v = get_cached_domain(domain, 'typo')
    if v is None:
        v = detect_typo(domain)
        set_cached_domain(domain, 'typo', v)
    return v

def _cached_catchall(domain):
    v = get_cached_domain(domain, 'catchall')
    if v is None:
        v = is_catchall(domain)
        set_cached_domain(domain, 'catchall', v)
    return v

def _cached_smtp(email):
    v = get_cached_smtp(email)
    if v is None:
        v = smtp_verify(email)
        set_cached_smtp(email, v)
    return v


# ─────────────────────────────────────────────────────────────
# MAIN VALIDATE FUNCTION  (cache-first)
# ─────────────────────────────────────────────────────────────

def validate_email(email):

    email = email.strip().lower()

    # ── 0. Return immediately if full result is cached ────────
    cached = get_cached_email_result(email)
    if cached is not None:
        cached['_cache'] = 'hit'
        return cached

    # ── 1. Syntax Validation ──────────────────────────────────
    syntax = bool(
        re.match(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$', email)
    )

    if not syntax:
        result = {
            "email":  email,
            "status": "invalid",
            "valid":  False,
            "score":  0,
            "reason": "Invalid email syntax",
        }
        set_cached_email_result(email, result)
        return result

    local_part, domain = email.split("@")

    # ── 2. DNS Checks (domain-level cache) ────────────────────
    mx    = _cached_mx(domain)
    spf   = _cached_spf(domain)
    dkim  = _cached_dkim(domain)
    dmarc = _cached_dmarc(domain)

    # ── 3. Email Intelligence (domain-level cache) ────────────
    typo         = _cached_typo(domain)
    disposable   = _cached_disposable(domain)
    role_account = is_role_account(email)   # fast local check – no network
    catchall     = _cached_catchall(domain)
    random_username = is_random_username(email)  # fast local

    # ── 4. SMTP Verification (per-email cache, shorter TTL) ───
    smtp_result = _cached_smtp(email)

    # ── 5. Domain Intelligence (domain-level cache) ───────────
    reputation = _cached_reputation(domain)
    domain_age = _cached_domain_age(domain)

    # ── 6. External API ───────────────────────────────────────
    # try:
    #     abstract_result = abstract_verify(email)
    # except Exception:
    #     abstract_result = None

    # ── 7. Score ──────────────────────────────────────────────
    score = calculate_score(
        syntax=syntax, mx=mx, spf=spf, dkim=dkim, dmarc=dmarc,
        disposable=disposable, typo=typo, catchall=catchall,
        role_account=role_account, random_username=random_username,
        reputation=reputation, domain_age=domain_age,
        smtp_result=smtp_result, 
        # abstract_result=abstract_result,
    )

    # ── 8. Status Classification ──────────────────────────────
    smtp_status = smtp_result.get("smtp_status")

    if not mx:
        status = "invalid"
    elif disposable:
        status = "disposable"
    elif typo:
        status = "typo_detected"
    elif smtp_status == "mailbox_does_not_exist":
        status = "undeliverable"
    elif smtp_status in [
        "verification_blocked", "greylisted_or_temporary_failure",
        "timeout", "connection_failed", "server_disconnected",
        "smtp_port_blocked", "smtp_verification_failed",
        "temporary_failure", "dns_timeout", "dns_failure",
        "smtp_deadline_exceeded",
    ]:
        status = "risky"
    elif catchall:
        status = "accept_all"
    elif score >= 80:
        status = "deliverable"
    elif score >= 50:
        status = "risky"
    else:
        status = "invalid"

    # ── 9. Valid Flag ─────────────────────────────────────────
    if status in ("deliverable", "accept_all"):
        valid = True
    elif status in ("invalid", "undeliverable", "disposable", "typo_detected"):
        valid = False
    else:
        valid = None

    # ── 10. Build & cache result ──────────────────────────────
    result = {
        "email":  email,
        "status": status,
        "valid":  valid,
        "score":  score,
        "mx":     mx,
        "spf":    spf,
        "dkim":   dkim,
        "dmarc":  dmarc,
        "smtp":   smtp_result,
        "catchall":        catchall,
        "disposable":      disposable,
        "role_account":    role_account,
        "random_username": random_username,
        "typo":            typo,
        "reputation":      reputation,
        "domain_age":      domain_age,
        # "abstract":        abstract_result,
    }

    set_cached_email_result(email, result)
    return result