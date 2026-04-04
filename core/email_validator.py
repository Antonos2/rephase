"""Blocca registrazioni da servizi di email temporanea."""

# Lista domini temp mail noti (aggiornabile)
TEMP_MAIL_DOMAINS = {
    "10minutemail.com", "10minmail.com", "20minutemail.com",
    "binkmail.com", "bobmail.info", "bofthew.com", "brefmail.com",
    "bugmenot.com", "bumpymail.com", "burnermail.io",
    "crazymailing.com", "cuirmail.com",
    "dispostable.com", "drdrb.com",
    "emailfake.com", "emailondeck.com", "emailtemporaire.com",
    "fakeinbox.com", "fakemail.net", "filzmail.com",
    "getairmail.com", "getnada.com", "grr.la", "guerrillamail.com",
    "guerrillamail.de", "guerrillamail.info", "guerrillamail.net",
    "guerrillamailblock.com",
    "harakirimail.com", "hidemail.de",
    "jetable.com",
    "koszmail.pl",
    "lhsdv.com", "linuxmail.so", "litedrop.com",
    "mailcatch.com", "maildrop.cc", "mailexpire.com", "mailfree.ga",
    "mailinator.com", "mailinator2.com", "mailismagic.com",
    "mailmate.com", "mailnesia.com", "mailnull.com",
    "mailpoof.com", "mailsac.com", "mailscrap.com",
    "mailslurp.com", "mailtemp.info", "mailtothis.com",
    "mohmal.com", "mt2015.com", "mytemp.email", "mytrashmail.com",
    "nada.email", "nwldx.com",
    "objectmail.com",
    "pookmail.com",
    "receiveee.com", "rtrtr.com",
    "sharklasers.com", "shieldedmail.com", "spamavert.com",
    "spambox.us", "spamcowboy.com", "spamfree24.org",
    "spamgourmet.com", "spamherelots.com",
    "temp-mail.io", "temp-mail.org", "tempail.com",
    "tempalias.com", "tempe4mail.com", "tempemail.co.za",
    "tempemail.net", "tempinbox.com", "tempmail.com",
    "tempmail.eu", "tempmail.it", "tempmail.net",
    "tempmailer.com", "tempomail.fr", "temporaryemail.net",
    "temporaryforwarding.com", "temporaryinbox.com",
    "temporarymailaddress.com", "thankdog.com",
    "throwam.com", "throwaway.email", "trashmail.at",
    "trashmail.com", "trashmail.me", "trashmail.net",
    "trashymail.com", "trbvm.com", "trbvn.com",
    "yopmail.com", "yopmail.fr",
    "zoemail.org",
    # Aggiungi altri domini qui
}

def is_temp_email(email: str) -> bool:
    """Ritorna True se l'email usa un dominio di posta temporanea."""
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].lower().strip()
    return domain in TEMP_MAIL_DOMAINS

def validate_email(email: str) -> dict:
    """Valida un indirizzo email. Ritorna {valid: bool, error: str|None}."""
    if not email or "@" not in email:
        return {"valid": False, "error": "Indirizzo email non valido"}
    email = email.strip().lower()
    local, domain = email.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        return {"valid": False, "error": "Formato email non valido"}
    if len(email) > 254 or len(local) > 64:
        return {"valid": False, "error": "Indirizzo email troppo lungo"}
    if is_temp_email(email):
        return {"valid": False, "error": "Non sono accettati indirizzi email temporanei o usa e getta"}
    return {"valid": True, "error": None}
