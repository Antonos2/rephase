"""OTP authentication — genera, invia e verifica codici a 6 cifre."""

import os
import secrets
import time
import threading
import resend

from core.email_validator import validate_email

# ── OTP store (in-memory, TTL 5 minuti) ──────────────────────────────────────
_otp_store = {}   # email → { code, expires, attempts }
_otp_lock  = threading.Lock()
OTP_TTL    = 300  # 5 minuti
OTP_MAX_ATTEMPTS = 5

# ── Session store (token → email) ────────────────────────────────────────────
_sessions_store = {}   # session_token → email
_sessions_lock  = threading.Lock()

# ── Quota conversioni Free ────────────────────────────────────────────────────
FREE_CONVERSIONS_MAX = 2
_conversions_store = {}   # email → numero conversioni usate
_conversions_lock  = threading.Lock()


def get_conversions_used(email: str) -> int:
    """Ritorna il numero di conversioni usate dall'utente (0 se non esiste)."""
    email = email.strip().lower()
    with _conversions_lock:
        return _conversions_store.get(email, 0)


def increment_conversions(email: str):
    """Incrementa il contatore conversioni per l'utente."""
    email = email.strip().lower()
    with _conversions_lock:
        _conversions_store[email] = _conversions_store.get(email, 0) + 1


def get_email_by_token(token: str):
    """Ritorna l'email associata al session token, o None."""
    with _sessions_lock:
        return _sessions_store.get(token)

def _cleanup_expired():
    now = time.time()
    with _otp_lock:
        expired = [k for k, v in _otp_store.items() if v["expires"] < now]
        for k in expired:
            del _otp_store[k]

def generate_otp(email: str) -> dict:
    """Valida email, genera OTP 6 cifre, invia via Resend.
    Ritorna {success, error?}."""

    # Valida email (blocca temp mail)
    check = validate_email(email)
    if not check["valid"]:
        return {"success": False, "error": check["error"]}

    email = email.strip().lower()

    # Rate limit: max 1 OTP per email ogni 60 secondi
    with _otp_lock:
        existing = _otp_store.get(email)
        if existing and existing["expires"] - OTP_TTL + 60 > time.time():
            return {"success": False, "error": "Attendi 60 secondi prima di richiedere un nuovo codice"}

    # Genera codice 6 cifre
    code = f"{secrets.randbelow(1000000):06d}"

    # DEV-mode: stampa OTP in chiaro sul terminale se in locale
    _base_url = os.environ.get("BASE_URL", "")
    if "localhost" in _base_url or "127.0.0.1" in _base_url:
        print(f"[DEV] OTP per {email}: {code}", flush=True)

    # Salva
    with _otp_lock:
        _otp_store[email] = {
            "code":     code,
            "expires":  time.time() + OTP_TTL,
            "attempts": 0,
        }

    # Invia via Resend
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        print(f"[auth] RESEND_API_KEY mancante — OTP per {email}: {code}", flush=True)
        return {"success": False, "error": "Servizio email non configurato"}

    resend.api_key = api_key
    from_email = os.environ.get("RESEND_FROM", "Rephase <noreply@rephase.app>")

    try:
        resend.Emails.send({
            "from":    from_email,
            "to":      [email],
            "subject": f"Rephase — Codice di verifica: {code}",
            "html":    (
                f'<div style="font-family:-apple-system,sans-serif;max-width:400px;margin:0 auto;padding:32px;">'
                f'<h2 style="color:#1a1a1a;">re<span style="color:#34c759;">phase</span></h2>'
                f'<p style="color:#333;font-size:15px;">Il tuo codice di verifica:</p>'
                f'<div style="background:#f0f0f0;border-radius:12px;padding:20px;text-align:center;margin:16px 0;">'
                f'<span style="font-size:32px;font-weight:700;letter-spacing:8px;color:#1a1a1a;">{code}</span>'
                f'</div>'
                f'<p style="color:#888;font-size:13px;">Il codice scade tra 5 minuti.</p>'
                f'<p style="color:#888;font-size:13px;">Se non hai richiesto questo codice, ignora questa email.</p>'
                f'</div>'
            ),
        })
        print(f"[auth] OTP inviato a {email[:3]}***@{email.split('@')[1]}", flush=True)
        return {"success": True}
    except Exception as e:
        print(f"[auth] Resend ERRORE: {e}", flush=True)
        return {"success": False, "error": "Errore invio email — riprova tra qualche secondo"}


def verify_otp(email: str, code: str) -> dict:
    """Verifica OTP. Ritorna {success, error?, session_token?}."""
    email = email.strip().lower()
    code  = code.strip()

    _cleanup_expired()

    with _otp_lock:
        entry = _otp_store.get(email)

    if not entry:
        return {"success": False, "error": "Codice scaduto o non richiesto — richiedi un nuovo codice"}

    if entry["expires"] < time.time():
        with _otp_lock:
            _otp_store.pop(email, None)
        return {"success": False, "error": "Codice scaduto — richiedi un nuovo codice"}

    if entry["attempts"] >= OTP_MAX_ATTEMPTS:
        with _otp_lock:
            _otp_store.pop(email, None)
        return {"success": False, "error": "Troppi tentativi — richiedi un nuovo codice"}

    with _otp_lock:
        _otp_store[email]["attempts"] += 1

    if entry["code"] != code:
        remaining = OTP_MAX_ATTEMPTS - entry["attempts"] - 1
        return {"success": False, "error": f"Codice errato — {remaining} tentativi rimanenti"}

    # Successo — elimina OTP e genera session token
    with _otp_lock:
        _otp_store.pop(email, None)

    session_token = secrets.token_urlsafe(32)
    with _sessions_lock:
        _sessions_store[session_token] = email
    print(f"[auth] OTP verificato per {email[:3]}***@{email.split('@')[1]}", flush=True)

    return {"success": True, "session_token": session_token, "email": email}
