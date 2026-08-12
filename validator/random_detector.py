import re


def is_random_username(email):

    username = email.split("@")[0].lower()

    score = 0

    # --------------------------
    # Known garbage patterns
    # --------------------------

    suspicious_patterns = [
        "asdf",
        "qwerty",
        "zxcv",
        "poiuy",
        "lkjh",
        "test",
        "temp",
        "dummy",
        "fake",
        "admin123",
        "abc123",
        "123456",
        "password"
    ]

    for pattern in suspicious_patterns:
        if pattern in username:
            score += 100

    # --------------------------
    # Excessive length
    # --------------------------

    if len(username) > 18:
        score += 25

    if len(username) > 25:
        score += 40

    # --------------------------
    # Too many digits
    # --------------------------

    digit_count = sum(c.isdigit() for c in username)

    if digit_count >= 4:
        score += 20

    if digit_count >= 6:
        score += 40

    # --------------------------
    # Mixed letters + digits
    # --------------------------

    has_letters = bool(re.search(r"[a-z]", username))
    has_digits = bool(re.search(r"\d", username))

    if has_letters and has_digits and len(username) >= 12:
        score += 15

    # --------------------------
    # Long digit sequence
    # --------------------------

    if re.search(r"\d{4,}", username):
        score += 25

    # --------------------------
    # Repeated characters
    # --------------------------

    if re.search(r"(.)\1{3,}", username):
        score += 25

    # --------------------------
    # Keyboard smash
    # --------------------------

    if re.search(
        r"[bcdfghjklmnpqrstvwxyz]{6,}",
        username
    ):
        score += 35

    # --------------------------
    # Random-looking character diversity
    # --------------------------

    unique_ratio = len(set(username)) / max(len(username), 1)

    if len(username) >= 12 and unique_ratio > 0.85:
        score += 25

    # --------------------------
    # Multiple alternating groups
    # --------------------------

    if re.search(
        r"[a-z]+\d+[a-z]+\d+[a-z]*",
        username
    ):
        score += 30

    # --------------------------
    # Consecutive consonants
    # --------------------------

    if re.search(
        r"[bcdfghjklmnpqrstvwxyz]{5,}",
        username
    ):
        score += 25

    # --------------------------
    # Consecutive random letters
    # Example:
    # xkqjztm
    # nbbdfigam
    # --------------------------

    vowels = len(re.findall(r"[aeiou]", username))

    if len(username) >= 10 and vowels <= 2:
        score += 35

    # --------------------------
    # Final Decision
    # --------------------------

    return score >= 50