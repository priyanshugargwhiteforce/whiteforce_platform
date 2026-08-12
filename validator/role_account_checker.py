ROLE_ACCOUNTS = {
    "admin",
    "support",
    "help",
    "sales",
    "billing",
    "noreply",
    "postmaster",
    "webmaster",
    "info",
    "contact"
}

def is_role_account(email):

    local = email.split("@")[0]

    return local.lower() in ROLE_ACCOUNTS