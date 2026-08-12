def calculate_score(
    syntax,
    mx,
    spf,
    dkim,
    dmarc,
    disposable,
    typo,
    catchall,
    role_account,
    reputation,
    domain_age,
    smtp_result,
    random_username,
    abstract_result=None
):

    score = 0

    # ------------------
    # Hard Fail Checks
    # ------------------

    smtp_status = smtp_result.get("smtp_status") if smtp_result else None

    if smtp_status == "mailbox_does_not_exist":
        return 0

    if not syntax:
        return 0

    if not mx:
        return 0

    # ------------------
    # Positive Signals
    # ------------------

    score += 10  # syntax

    score += 20  # mx

    if spf:
        score += 5

    if dkim:
        score += 5

    if dmarc:
        score += 5

    # ------------------
    # SMTP Weighting
    # ------------------

    if smtp_status == "mailbox_exists_likely":
        score += 30

    elif smtp_status == "verification_blocked":
        score += 10

    elif smtp_status == "greylisted_or_temporary_failure":
        score += 5

    elif smtp_status == "timeout":
        score += 3

    # ------------------
    # Reputation
    # ------------------

    score += min(reputation, 20)

    score += min(domain_age, 20)

    # ------------------
    # Abstract API
    # ------------------

    if abstract_result:

        deliverability = abstract_result.get(
            "deliverability"
        )

        if deliverability == "DELIVERABLE":
            score += 15

        elif deliverability == "UNDELIVERABLE":
            return 0

        elif deliverability == "RISKY":
            score -= 10

    # ------------------
    # Penalties
    # ------------------

    if disposable:
        score -= 40

    if typo:
        score -= 25

    if catchall:
        score -= 15

    if role_account:
        score -= 15

    if random_username:
        score -= 30

    # ------------------
    # Final Clamp
    # ------------------

    score = max(0, min(score, 100))

    return score
