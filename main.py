#!/usr/bin/env python3
# Rephase API — pitch engine: Rubber Band R3 → ffmpeg librubberband → SoX
import os, uuid, threading
import stripe
import time as _time
import hashlib
import logging
import psutil
import aiofiles
from pathlib import Path
from collections import deque
from datetime import datetime, timezone
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Depends, Header, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from core.converter import convert_to_432, analyze_file

# ══════════════════════════════════════════════════════════════════════════════
# 🚦 VIGILE URBANO — logger interno Rephase
# ══════════════════════════════════════════════════════════════════════════════

# Log strutturato su file (rotazione manuale ogni 5000 eventi)
_log_handler = logging.FileHandler("rephase_events.log")
_log_handler.setFormatter(logging.Formatter("%(message)s"))
_vigile = logging.getLogger("vigile")
_vigile.setLevel(logging.INFO)
_vigile.addHandler(_log_handler)
_vigile.propagate = False

# Deque in memoria — ultimi 5000 eventi (zero memoria aggiuntiva rilevante)
_event_log: deque = deque(maxlen=5000)

# Contatori globali
_active_conversions: int = 0
_active_verifies: int = 0
_active_analyses: int = 0
_counters_lock = threading.Lock()

# ── Blacklist Free SQLite (lookup O(1) per email) ─────────────────────────────
import sqlite3

# Path persistente: su Render il disco è montato in /data, in locale fallback ./rephase.db
def _resolve_db_path():
    override = os.environ.get("REPHASE_DB_PATH", "")
    if override:
        return override
    if os.path.isdir("/data"):
        return "/data/rephase.db"
    return "rephase.db"

_BLACKLIST_DB_PATH = _resolve_db_path()
_blacklist_lock = threading.Lock()

def _blacklist_init():
    conn = sqlite3.connect(_BLACKLIST_DB_PATH)
    try:
        # email è PRIMARY KEY → indice automatico, lookup O(1)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS free_exhausted (
                email TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    finally:
        conn.close()
    print(f"[blacklist] SQLite db path = {_BLACKLIST_DB_PATH}", flush=True)

_blacklist_init()

def _abbonati_init():
    """Tabella abbonati: email, piano, date, importo, contatori uso, username.
    Migra automaticamente schemi precedenti aggiungendo colonne mancanti."""
    conn = sqlite3.connect(_BLACKLIST_DB_PATH)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS abbonati (
                email TEXT NOT NULL,
                piano TEXT NOT NULL,
                data_iscrizione TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                data_scadenza TIMESTAMP,
                importo_chf REAL,
                stripe_event_id TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                verifiche_totali INTEGER DEFAULT 0,
                conversioni_totali INTEGER DEFAULT 0,
                username TEXT,
                PRIMARY KEY (email, piano)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_abbonati_email ON abbonati(email)")

        # Migrazione: aggiungi colonne mancanti per DB pre-esistenti
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(abbonati)").fetchall()}
        for col, ddl in [
            ("verifiche_totali",   "ALTER TABLE abbonati ADD COLUMN verifiche_totali INTEGER DEFAULT 0"),
            ("conversioni_totali", "ALTER TABLE abbonati ADD COLUMN conversioni_totali INTEGER DEFAULT 0"),
            ("username",           "ALTER TABLE abbonati ADD COLUMN username TEXT"),
        ]:
            if col not in existing_cols:
                try:
                    conn.execute(ddl)
                    print(f"[abbonati] migrazione: aggiunta colonna {col}", flush=True)
                except Exception as ex:
                    print(f"[abbonati] migrazione errore {col}: {ex}", flush=True)
        conn.commit()
    finally:
        conn.close()
    print(f"[abbonati] SQLite tabella inizializzata", flush=True)

def _generate_username():
    """Genera username random nel formato rephase_XXXXX (5 char alfanumerici lowercase)."""
    import random, string
    chars = string.ascii_lowercase + string.digits
    return "rephase_" + "".join(random.choices(chars, k=5))

def _get_username_for_email(conn, email_norm):
    """Ritorna username esistente per email (qualsiasi piano), o None."""
    row = conn.execute(
        "SELECT username FROM abbonati WHERE email = ? AND username IS NOT NULL LIMIT 1",
        (email_norm,)
    ).fetchone()
    return row[0] if row and row[0] else None

_abbonati_init()

def _log_operazioni_init():
    """Tabella log_operazioni: log anonimo (hash) di verifiche e conversioni."""
    conn = sqlite3.connect(_BLACKLIST_DB_PATH)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS log_operazioni (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                email_hash TEXT,
                piano TEXT,
                tipo TEXT,
                timestamp TEXT,
                ip_hash TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_log_op_timestamp ON log_operazioni(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_log_op_tipo ON log_operazioni(tipo)")
        conn.commit()
    finally:
        conn.close()
    print(f"[log_operazioni] tabella inizializzata", flush=True)

_log_operazioni_init()

def _log_operazione(email, piano, tipo, request=None):
    """Inserisce una riga in log_operazioni. email/ip vengono hashati (no PII).
    Recupera username dalla tabella abbonati se l'email è nota."""
    try:
        email_norm = (email or "").strip().lower()
        # Hash email (primi 8 char SHA256) — pseudonimizzazione
        email_hash = hashlib.sha256(email_norm.encode("utf-8")).hexdigest()[:8] if email_norm else None
        # Hash IP completo
        client_ip = ""
        if request is not None:
            try:
                client_ip = request.headers.get("x-forwarded-for", "") or (request.client.host if request.client else "")
                if client_ip and "," in client_ip:
                    client_ip = client_ip.split(",")[0].strip()
            except Exception:
                client_ip = ""
        ip_hash = hashlib.sha256(client_ip.encode("utf-8")).hexdigest() if client_ip else None
        # Lookup username (se utente è in abbonati)
        username = None
        if email_norm:
            with _blacklist_lock:
                conn = sqlite3.connect(_BLACKLIST_DB_PATH)
                try:
                    row = conn.execute(
                        "SELECT username FROM abbonati WHERE email = ? AND username IS NOT NULL LIMIT 1",
                        (email_norm,)
                    ).fetchone()
                    if row:
                        username = row[0]
                finally:
                    conn.close()
        ts = datetime.now(timezone.utc).isoformat()
        with _blacklist_lock:
            conn = sqlite3.connect(_BLACKLIST_DB_PATH)
            try:
                conn.execute("""
                    INSERT INTO log_operazioni (username, email_hash, piano, tipo, timestamp, ip_hash)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (username, email_hash, piano or "free", tipo, ts, ip_hash))
                conn.commit()
            finally:
                conn.close()
    except Exception as ex:
        print(f"[log_operazioni] errore insert: {type(ex).__name__}: {ex}", flush=True)

def upsert_abbonato(email, piano, data_scadenza=None, importo_chf=None, stripe_event_id=None):
    """Inserisce o aggiorna un abbonato. Idempotente: se (email, piano) esiste,
    aggiorna data_scadenza, importo e updated_at; data_iscrizione resta originale.
    Garantisce che ogni email abbia uno username univoco generato al primo signup."""
    if not email or not piano:
        return
    e = email.strip().lower()
    with _blacklist_lock:
        conn = sqlite3.connect(_BLACKLIST_DB_PATH)
        try:
            # Riusa username esistente per questa email, oppure generane uno nuovo
            username = _get_username_for_email(conn, e)
            if not username:
                # Loop di sicurezza per evitare collisioni (estremamente rare)
                for _ in range(10):
                    candidate = _generate_username()
                    exists = conn.execute(
                        "SELECT 1 FROM abbonati WHERE username = ? LIMIT 1", (candidate,)
                    ).fetchone()
                    if not exists:
                        username = candidate
                        break
                if not username:
                    username = _generate_username()  # fallback (collisione improbabile)

            conn.execute("""
                INSERT INTO abbonati (email, piano, data_scadenza, importo_chf, stripe_event_id, updated_at, username)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                ON CONFLICT(email, piano) DO UPDATE SET
                    data_scadenza = excluded.data_scadenza,
                    importo_chf   = excluded.importo_chf,
                    stripe_event_id = excluded.stripe_event_id,
                    updated_at    = CURRENT_TIMESTAMP,
                    username      = COALESCE(abbonati.username, excluded.username)
            """, (e, piano, data_scadenza, importo_chf, stripe_event_id, username))
            conn.commit()
        finally:
            conn.close()
    print(f"[abbonati] upsert email={e} piano={piano} username={username} scadenza={data_scadenza} importo={importo_chf}", flush=True)

def get_username_by_email(email):
    """Lookup username per email. Se non esiste record in abbonati, ne crea uno
    'placeholder' (piano='free') solo per assegnare lo username e ritornarlo."""
    if not email:
        return None
    e = email.strip().lower()
    with _blacklist_lock:
        conn = sqlite3.connect(_BLACKLIST_DB_PATH)
        try:
            existing = _get_username_for_email(conn, e)
            if existing:
                return existing
            # Nessun record per questa email → crea placeholder con piano='free'
            for _ in range(10):
                candidate = _generate_username()
                if not conn.execute("SELECT 1 FROM abbonati WHERE username = ? LIMIT 1", (candidate,)).fetchone():
                    break
            conn.execute(
                "INSERT OR IGNORE INTO abbonati (email, piano, username) VALUES (?, 'free', ?)",
                (e, candidate)
            )
            conn.commit()
            return candidate
        finally:
            conn.close()

def increment_verifica_abbonato(email):
    """Incrementa verifiche_totali per email (no-op se email non esiste in abbonati)."""
    if not email:
        return
    e = email.strip().lower()
    with _blacklist_lock:
        conn = sqlite3.connect(_BLACKLIST_DB_PATH)
        try:
            conn.execute("UPDATE abbonati SET verifiche_totali = verifiche_totali + 1 WHERE email = ?", (e,))
            conn.commit()
        finally:
            conn.close()

def increment_conversione_abbonato(email):
    """Incrementa conversioni_totali per email (no-op se email non esiste in abbonati)."""
    if not email:
        return
    e = email.strip().lower()
    with _blacklist_lock:
        conn = sqlite3.connect(_BLACKLIST_DB_PATH)
        try:
            conn.execute("UPDATE abbonati SET conversioni_totali = conversioni_totali + 1 WHERE email = ?", (e,))
            conn.commit()
        finally:
            conn.close()

def _get_plan_from_db(email):
    """Cerca un piano attivo per email nella tabella abbonati. Ritorna (plan, scadenza_iso, verifiche, conversioni)
    o (None, None, 0, 0) se non trovato. Lifetime (data_scadenza NULL) ha priorità sui piani con scadenza."""
    if not email:
        return (None, None, 0, 0)
    e = email.strip().lower()
    with _blacklist_lock:
        conn = sqlite3.connect(_BLACKLIST_DB_PATH)
        try:
            # Lifetime (NULL) sempre prioritario, poi pro più "fresco" (scadenza più lontana)
            row = conn.execute("""
                SELECT piano, data_scadenza, verifiche_totali, conversioni_totali
                FROM abbonati
                WHERE email = ?
                  AND (data_scadenza IS NULL OR data_scadenza > datetime('now'))
                ORDER BY (data_scadenza IS NULL) DESC, data_scadenza DESC
                LIMIT 1
            """, (e,)).fetchone()
        finally:
            conn.close()
    if not row:
        return (None, None, 0, 0)
    return (row[0], row[1], row[2] or 0, row[3] or 0)

def _migrate_exhausted_emails():
    """Migra all'avvio le email che hanno già esaurito le 2 conversioni gratuite
    nel sistema in-memory (_conversions_store) verso la blacklist SQLite.
    Idempotente: chiamabile a ogni avvio. Salta utenti Pro/Lifetime."""
    try:
        from core.auth import _conversions_store, _conversions_lock, FREE_CONVERSIONS_MAX
        with _conversions_lock:
            snapshot = list(_conversions_store.items())
        if not snapshot:
            print("[blacklist] migrate: nessuna email da migrare (store vuoto)", flush=True)
            return
        migrated = 0
        skipped_pro = 0
        for email, used in snapshot:
            if used < FREE_CONVERSIONS_MAX:
                continue
            try:
                if _get_user_plan(email) != "free":
                    skipped_pro += 1
                    continue
                mark_free_exhausted(email)
                migrated += 1
            except Exception as ex:
                print(f"[blacklist] migrate error per {email}: {type(ex).__name__}: {ex}", flush=True)
        print(f"[blacklist] migrate: {migrated} email migrate, {skipped_pro} pro/lifetime saltate", flush=True)
    except Exception as e:
        print(f"[blacklist] _migrate_exhausted_emails error: {type(e).__name__}: {e}", flush=True)

def is_free_exhausted(email: str) -> bool:
    """Lookup O(1) sulla blacklist Free."""
    if not email:
        return False
    e = email.strip().lower()
    with _blacklist_lock:
        conn = sqlite3.connect(_BLACKLIST_DB_PATH)
        try:
            cur = conn.execute("SELECT 1 FROM free_exhausted WHERE email = ? LIMIT 1", (e,))
            return cur.fetchone() is not None
        finally:
            conn.close()

def mark_free_exhausted(email: str):
    """Inserisce email in blacklist (idempotente)."""
    if not email:
        return
    e = email.strip().lower()
    with _blacklist_lock:
        conn = sqlite3.connect(_BLACKLIST_DB_PATH)
        try:
            conn.execute("INSERT OR IGNORE INTO free_exhausted(email) VALUES (?)", (e,))
            conn.commit()
        finally:
            conn.close()
    print(f"[blacklist] free_exhausted += {e}", flush=True)

def unmark_free_exhausted(email: str):
    """Rimuove email dalla blacklist (es. dopo upgrade Pro)."""
    if not email:
        return
    e = email.strip().lower()
    with _blacklist_lock:
        conn = sqlite3.connect(_BLACKLIST_DB_PATH)
        try:
            conn.execute("DELETE FROM free_exhausted WHERE email = ?", (e,))
            conn.commit()
        finally:
            conn.close()


def _log_event(
    event_type: str,        # "verify" | "convert" | "convert_sync" | "analyze"
    esito: str,             # "ok" | "error" | "timeout" | "already_432"
    duration_sec: float,
    file_size_mb: float,
    formato: str,
    request: Request,
    extra: dict = None,
):
    """Registra un evento nel log interno. Chiamato nel finally di ogni endpoint."""
    ip_raw = ""
    try:
        ip_raw = request.client.host or ""
    except Exception:
        pass
    ip_hash = hashlib.sha256(ip_raw.encode()).hexdigest()[:12]

    try:
        mem_pct = psutil.virtual_memory().percent
        cpu_pct = psutil.cpu_percent(interval=None)   # non-blocking
    except Exception:
        mem_pct = cpu_pct = -1.0

    with _counters_lock:
        active_snap = {
            "conversions": _active_conversions,
            "verifies":    _active_verifies,
            "analyses":    _active_analyses,
        }

    entry = {
        "ts":           datetime.now(timezone.utc).isoformat(),
        "type":         event_type,
        "esito":        esito,
        "duration_sec": round(duration_sec, 2),
        "file_mb":      round(file_size_mb, 3),
        "format":       formato,
        "ip_hash":      ip_hash,
        "ua":           (request.headers.get("user-agent", "") or "")[:80],
        "active":       active_snap,
        "mem_pct":      mem_pct,
        "cpu_pct":      cpu_pct,
        "extra":        extra or {},
    }

    _event_log.append(entry)
    _vigile.info(entry)


# ── In-memory job store (analysis only) ──────────────────────────────────────
_jobs: dict = {}
_jobs_lock = threading.Lock()


def _cleanup_jobs():
    """Background thread: remove completed/failed analysis jobs older than 30 min."""
    while True:
        _time.sleep(300)
        cutoff = _time.time() - 1800
        with _jobs_lock:
            expired = [k for k, v in _jobs.items()
                       if v.get("status") in ("done", "error")
                       and v.get("last_accessed", v.get("created_at", 0)) < cutoff]
            for k in expired:
                del _jobs[k]


threading.Thread(target=_cleanup_jobs, daemon=True).start()

# ── Startup diagnostic: log sox/ffmpeg paths immediately ─────────────────────
import shutil as _shutil, subprocess as _sp_startup
def _log_tool(name):
    p = _shutil.which(name)
    if not p:
        try:
            r = _sp_startup.run(["which", name], capture_output=True, text=True, timeout=5)
            p = r.stdout.strip() or None
        except Exception:
            pass
    print(f"[startup] {name}: {p or 'NOT FOUND'}", flush=True)
_log_tool("rubberband")
_log_tool("sox")
_log_tool("ffmpeg")

app = FastAPI(title="Rephase API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

TEMP_DIR = Path("/tmp/rephase")
TEMP_DIR.mkdir(exist_ok=True)
ALLOWED = {".mp3",".m4a",".wav",".flac",".aac",".aiff"}
MAX_SIZE = 500*1024*1024

def cleanup(path):
    import time; time.sleep(60)
    if os.path.exists(path): os.remove(path)

# ── Job system per conversione asincrona ──────────────────────────────────────
import json as _json
JOBS_DIR = TEMP_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

def _job_path(job_id):
    return JOBS_DIR / f"{job_id}.json"

def _save_job(job_id, data):
    with open(_job_path(job_id), "w") as f:
        _json.dump(data, f)

def _load_job(job_id):
    p = _job_path(job_id)
    if not p.exists():
        return None
    with open(p) as f:
        return _json.load(f)

def _get_user_plan_details(email):
    """Ritorna (plan, subscription_end_iso). plan ∈ {'free','pro_monthly','pro_annual','lifetime'}.
    subscription_end_iso è ISO UTC solo per pro_monthly/pro_annual; None altrimenti.

    DB-first: prima cerca nella tabella abbonati locale (zero chiamate Stripe se trovato).
    Fallback su Stripe solo se nessun record locale valido.
    """
    if not email:
        return ("free", None)

    email_norm = email.strip().lower()

    # ── Step 1: lookup nel DB locale ──
    db_plan, db_scadenza, db_verifiche, db_conv = _get_plan_from_db(email_norm)
    if db_plan:
        scadenza_fmt = db_scadenza or "lifetime"
        print(f"[plan] DB locale: email={email_norm} piano={db_plan} scade={scadenza_fmt} verifiche={db_verifiche} conversioni={db_conv}", flush=True)
        # Per lifetime ritorniamo None come scadenza (coerente con semantica esistente)
        return (db_plan, db_scadenza if db_plan != "lifetime" else None)

    # ── Step 2: fallback Stripe ──
    stripe_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not stripe_key:
        return ("free", None)
    price_annual = os.environ.get("STRIPE_PRICE_ANNUAL", "")
    price_monthly = os.environ.get("STRIPE_PRICE_MONTHLY", "")

    try:
        stripe.api_key = stripe_key
        customers = stripe.Customer.list(email=email_norm, limit=10)
        if not customers.data:
            print(f"[auth] _get_user_plan({email_norm}): nessun customer Stripe → free", flush=True)
            return ("free", None)

        found_lifetime = False

        for cust in customers.data:
            cust_email = (cust.email or "").strip().lower()
            if cust_email != email_norm:
                continue
            cust_id = cust.id

            # ── Check 1: subscription attiva REALMENTE pagata ──
            subs = stripe.Subscription.list(customer=cust_id, status="active", limit=10)
            for sub in subs.data:
                try:
                    invoices = stripe.Invoice.list(subscription=sub.id, status="paid", limit=1)
                    if not invoices.data:
                        print(f"[auth] _get_user_plan({email_norm}): sub {sub.id} active ma 0 invoice paid → ignorata", flush=True)
                        continue
                except Exception as ex:
                    print(f"[auth] _get_user_plan: invoice check error: {type(ex).__name__}: {ex}", flush=True)
                    continue

                sub_price = ""
                try:
                    sub_price = sub["items"]["data"][0]["price"]["id"]
                except (KeyError, IndexError, TypeError) as ex:
                    print(f"[auth] _get_user_plan: price ID non leggibile: {type(ex).__name__}: {ex}", flush=True)

                # Estrai data di scadenza dalla subscription (current_period_end)
                end_iso = None
                try:
                    end_unix = sub["current_period_end"]
                    end_iso = datetime.fromtimestamp(end_unix, tz=timezone.utc).isoformat()
                except (KeyError, TypeError, ValueError):
                    end_iso = None

                if sub_price == price_annual:
                    print(f"[auth] _get_user_plan({email_norm}): pro_annual end={end_iso}", flush=True)
                    return ("pro_annual", end_iso)
                if sub_price == price_monthly:
                    print(f"[auth] _get_user_plan({email_norm}): pro_monthly end={end_iso}", flush=True)
                    return ("pro_monthly", end_iso)
                print(f"[auth] _get_user_plan({email_norm}): WARN price '{sub_price}' non in env vars → assumo pro_monthly", flush=True)
                return ("pro_monthly", end_iso)

            # ── Check 2: lifetime (one-time payment riuscito) ──
            sessions = stripe.checkout.Session.list(customer=cust_id, limit=20)
            for s in sessions.data:
                if s.payment_status == "paid" and s.mode == "payment":
                    found_lifetime = True
                    break

        if found_lifetime:
            print(f"[auth] _get_user_plan({email_norm}): lifetime", flush=True)
            return ("lifetime", None)

        print(f"[auth] _get_user_plan({email_norm}): nessuna sub pagata né lifetime → free", flush=True)
        return ("free", None)

    except Exception as e:
        import traceback
        print(f"[auth] _get_user_plan error: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        return ("free", None)


def _get_user_plan(email):
    """Wrapper retro-compatibile: ritorna solo il piano (string)."""
    plan, _end = _get_user_plan_details(email)
    return plan

# Migrazione blacklist all'avvio (richiede _get_user_plan e mark_free_exhausted già definiti)
_migrate_exhausted_emails()

def _run_conversion(job_id, tmp_in, tmp_out, fmt, filename, file_mb, duration_sec, sox_timeout, cleanup_input=True):
    """Eseguita in background thread — aggiorna il job file."""
    try:
        # Recupera email autenticata dal job (se presente)
        _job_data = _load_job(job_id) or {}
        _conv_auth_email = _job_data.get("_auth_email")

        _save_job(job_id, {"status": "analyzing", "progress": 10})
        pre_check = analyze_file(tmp_in)
        if pre_check.get("is_432"):
            _save_job(job_id, {
                "status": "done", "already_432": True,
                "peak_freq": pre_check["peak_freq"],
                "cents_vs_432": pre_check["cents_vs_432"],
            })
            if cleanup_input and os.path.exists(tmp_in): os.remove(tmp_in)
            return

        _save_job(job_id, {"status": "converting", "progress": 30})
        result = convert_to_432(tmp_in, tmp_out, max_seconds=360, sox_timeout=sox_timeout)

        if not result["success"]:
            _save_job(job_id, {"status": "error", "error": result.get("error", "Errore sconosciuto")})
            return

        # Incrementa quota conversioni se utente autenticato
        if _conv_auth_email:
            from core.auth import increment_conversions, get_conversions_used, FREE_CONVERSIONS_MAX
            increment_conversions(_conv_auth_email)
            # Stats abbonati: incrementa conversioni_totali (no-op se non in tabella)
            try:
                increment_conversione_abbonato(_conv_auth_email)
            except Exception as ex:
                print(f"[abbonati] increment_conversione errore: {ex}", flush=True)
            # Log operazione (request=None, è un thread background — IP non disponibile)
            try:
                _c_plan = _get_user_plan(_conv_auth_email)
                _log_operazione(_conv_auth_email, _c_plan, "conversione", None)
            except Exception as ex:
                print(f"[log_operazioni] convert async errore: {ex}", flush=True)
            # Se l'utente Free ha raggiunto il limite → blacklist
            try:
                if get_conversions_used(_conv_auth_email) >= FREE_CONVERSIONS_MAX:
                    if _get_user_plan(_conv_auth_email) == "free":
                        mark_free_exhausted(_conv_auth_email)
            except Exception as ex:
                print(f"[blacklist] errore mark async: {type(ex).__name__}: {ex}", flush=True)

        _save_job(job_id, {
            "status": "done",
            "already_432": False,
            "filename": filename,
            "format": fmt,
            "output_path": tmp_out,
            "pre_freq": round(result.get("pre_freq", 0), 4),
            "shift_applied": round(result.get("shift_applied", 0), 4),
            "post_freq": round(result.get("post_freq", 0), 4),
            "post_cents_vs_432": round(result.get("post_cents_vs_432", 0), 4),
            "certified": result.get("certified", False),
            "correction_passes": result.get("correction_passes", 1),
            "engine": result.get("engine", "unknown"),
            "corr_pass_error": result.get("corr_pass_error"),
            "audio_duration_sec": round(duration_sec, 1),
        })
    except Exception as e:
        import traceback
        print(f"[convert/job] {job_id} FATAL:\n{traceback.format_exc()}", flush=True)
        _save_job(job_id, {"status": "error", "error": str(e)})
    finally:
        if cleanup_input and os.path.exists(tmp_in):
            try: os.remove(tmp_in)
            except Exception: pass

@app.get("/")
def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/app", status_code=301)

@app.get("/health")
def health():
    import shutil, subprocess as _sp
    def _resolve(name):
        p = shutil.which(name)
        if p: return p
        try:
            r = _sp.run(["which", name], capture_output=True, text=True, timeout=5)
            p = r.stdout.strip()
            if p: return p
        except Exception:
            pass
        hc = f"/usr/bin/{name}"
        return hc if os.path.isfile(hc) else None
    rb_path     = _resolve("rubberband")
    sox_path    = _resolve("sox")
    ffmpeg_path = _resolve("ffmpeg")
    engine      = "rubberband" if rb_path else ("sox" if sox_path else "none")
    print(f"[health] rubberband={rb_path}  sox={sox_path}  ffmpeg={ffmpeg_path}  engine={engine}", flush=True)
    return {
        "status":          "ok",
        "hostname":        os.environ.get("HOSTNAME", "unknown"),
        "engine":          engine,
        "rubberband":      rb_path is not None,
        "rubberband_path": rb_path,
        "sox":             sox_path is not None,
        "sox_path":        sox_path,
        "ffmpeg":          ffmpeg_path is not None,
        "ffmpeg_path":     ffmpeg_path,
    }

# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINT: /status — stato attuale del server (usato dal frontend pre-upload)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/status")
def server_status():
    """Frontend lo chiama prima del caricamento per avvisare l'utente se busy."""
    MAX_CONV = int(os.environ.get("MAX_CONCURRENT_CONVERSIONS", "3"))
    with _counters_lock:
        active_conv = _active_conversions
        active_ver  = _active_verifies
        active_ana  = _active_analyses

    # Stima durata media dalle ultime 50 conversioni ok
    recent = [e for e in list(_event_log)[-200:]
              if e["type"] in ("convert", "convert_sync") and e["esito"] == "ok"]
    avg_sec = (sum(e["duration_sec"] for e in recent[-50:]) / len(recent[-50:])) if recent else 60.0

    try:
        mem_pct = psutil.virtual_memory().percent
        cpu_pct = psutil.cpu_percent(interval=None)
    except Exception:
        mem_pct = cpu_pct = -1.0

    busy = active_conv >= MAX_CONV

    return {
        "busy":              busy,
        "active_conversions": active_conv,
        "active_verifies":   active_ver,
        "active_analyses":   active_ana,
        "max_conversions":   MAX_CONV,
        "queue_depth":       max(0, active_conv - MAX_CONV),
        "avg_duration_sec":  round(avg_sec, 1),
        "mem_pct":           mem_pct,
        "cpu_pct":           cpu_pct,
    }


@app.post("/verify")
async def verify(request: Request, background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    global _active_verifies
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Formato non supportato: {ext}")

    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(413, "File troppo grande")
    file_mb = len(content) / 1_000_000

    # Recupera email autenticata (se presente) per incrementare verifiche_totali abbonati
    _verify_email = None
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        from core.auth import get_email_by_token
        _verify_email = get_email_by_token(authorization[7:])

    tmp = str(TEMP_DIR / f"{uuid.uuid4()}{ext}")
    async with aiofiles.open(tmp, 'wb') as f:
        await f.write(content)

    with _counters_lock:
        _active_verifies += 1

    t0    = _time.time()
    esito = "ok"
    try:
        result = analyze_file(tmp)
        background_tasks.add_task(cleanup, tmp)
        if not result["success"]:
            esito = "error"
            raise HTTPException(500, result.get("error", "Errore"))
        # Incrementa contatore verifiche per abbonati (no-op se non in tabella)
        if _verify_email:
            try:
                increment_verifica_abbonato(_verify_email)
            except Exception as ex:
                print(f"[abbonati] increment_verifica errore: {ex}", flush=True)
        # Log operazione (anche per guest senza email)
        try:
            _verify_plan = _get_user_plan(_verify_email) if _verify_email else "free"
            _log_operazione(_verify_email, _verify_plan, "verifica", request)
        except Exception as ex:
            print(f"[log_operazioni] verify errore: {ex}", flush=True)
        return {
            **result,
            "filename": file.filename,
            "message":  "Certificato a 432 Hz ✅" if result["is_432"]
                        else "Questo brano è a 440 Hz — vuoi convertirlo?",
        }
    except HTTPException:
        esito = "error"
        raise
    except Exception as e:
        esito = "error"
        raise HTTPException(500, str(e))
    finally:
        with _counters_lock:
            _active_verifies -= 1
        _log_event(
            event_type="verify",
            esito=esito,
            duration_sec=_time.time() - t0,
            file_size_mb=file_mb,
            formato=ext.lstrip("."),
            request=request,
        )




# ── Synchronous conversion (no job state, no polling) ─────────────────────────

@app.post("/convert/sync")
async def convert_sync(request: Request, background_tasks: BackgroundTasks, file: UploadFile = File(...), format: str = "mp3"):
    global _active_conversions

    # ── Quota check ──
    from core.auth import get_email_by_token, get_conversions_used, increment_conversions, FREE_CONVERSIONS_MAX
    _auth_email = None
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        _auth_email = get_email_by_token(authorization[7:])
    if _auth_email:
        _user_plan = _get_user_plan(_auth_email)
        if _user_plan == "free" and get_conversions_used(_auth_email) >= FREE_CONVERSIONS_MAX:
            return JSONResponse(
                {"error": "Hai esaurito le 2 conversioni gratuite. Abbonati a Pro per conversioni illimitate."},
                status_code=403,
            )

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Formato non supportato: {ext}")
    if format not in ["mp3", "m4a"]:
        format = "mp3"

    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(413, "File troppo grande")
    file_mb = len(content) / 1_000_000

    uid     = str(uuid.uuid4())
    tmp_in  = str(TEMP_DIR / f"{uid}_in{ext}")
    tmp_out = str(TEMP_DIR / f"{uid}_432.{format}")
    with open(tmp_in, "wb") as fh:
        fh.write(content)

    with _counters_lock:
        _active_conversions += 1

    t0    = _time.time()
    esito = "ok"
    extra = {}
    try:
        import asyncio, functools
        loop = asyncio.get_event_loop()

        pre_check = await loop.run_in_executor(None, analyze_file, tmp_in)
        if pre_check.get("is_432"):
            esito = "already_432"
            return JSONResponse({
                "already_432":  True,
                "peak_freq":    pre_check["peak_freq"],
                "cents_vs_432": pre_check["cents_vs_432"],
                "message":      "Il brano è già a 432 Hz — nessuna conversione necessaria.",
            })

        from core.converter import _get_duration
        duration_sec = _get_duration(tmp_in) or 0.0
        sox_timeout  = max(120, int(duration_sec * 3))
        print(f"[convert/sync] duration={duration_sec:.1f}s  sox_timeout={sox_timeout}s", flush=True)

        result = await loop.run_in_executor(
            None,
            functools.partial(convert_to_432, tmp_in, tmp_out, max_seconds=360, sox_timeout=sox_timeout),
        )
        if not result["success"]:
            esito = "error"
            raise HTTPException(500, result.get("error", "Errore sconosciuto"))

        extra = {
            "shift_cents":       round(result.get("shift_applied", 0), 4),
            "post_cents_vs_432": round(result.get("post_cents_vs_432", 0), 4),
            "certified":         result.get("certified", False),
            "passes":            result.get("correction_passes", 1),
            "audio_duration_sec": round(duration_sec, 1),
        }

        stem    = Path(file.filename).stem
        exposed = "X-Pre-Freq,X-Shift-Cents,X-Post-Freq,X-Post-Cents,X-Certified,X-Correction-Passes"
        stats_headers = {
            "X-Pre-Freq":          str(round(result.get("pre_freq", 0), 4)),
            "X-Shift-Cents":       str(round(result.get("shift_applied", 0), 4)),
            "X-Post-Freq":         str(round(result.get("post_freq", 0), 4)),
            "X-Post-Cents":        str(round(result.get("post_cents_vs_432", 0), 4)),
            "X-Certified":         str(result.get("certified", False)),
            "X-Correction-Passes": str(round(result.get("correction_passes", 1))),
            "Access-Control-Expose-Headers": exposed,
        }
        if result.get("corr_pass_error"):
            stats_headers["X-Corr-Pass-Error"] = result["corr_pass_error"][:200]
            stats_headers["Access-Control-Expose-Headers"] += ",X-Corr-Pass-Error"

        if _auth_email:
            increment_conversions(_auth_email)
            try:
                increment_conversione_abbonato(_auth_email)
            except Exception as ex:
                print(f"[abbonati] increment_conversione errore: {ex}", flush=True)
            try:
                _cs_plan = _get_user_plan(_auth_email)
                _log_operazione(_auth_email, _cs_plan, "conversione", request)
            except Exception as ex:
                print(f"[log_operazioni] convert sync errore: {ex}", flush=True)
            try:
                if get_conversions_used(_auth_email) >= FREE_CONVERSIONS_MAX:
                    if _get_user_plan(_auth_email) == "free":
                        mark_free_exhausted(_auth_email)
            except Exception as ex:
                print(f"[blacklist] errore mark sync: {type(ex).__name__}: {ex}", flush=True)

        background_tasks.add_task(cleanup, tmp_out)
        return FileResponse(
            path=tmp_out,
            filename=f"{stem}_432.{format}",
            media_type="audio/mpeg" if format == "mp3" else "audio/mp4",
            headers=stats_headers,
        )
    except HTTPException:
        esito = "error"
        raise
    except Exception as e:
        import traceback
        print(f"[convert/sync] Exception:\n{traceback.format_exc()}", flush=True)
        esito = "error"
        raise HTTPException(500, str(e))
    finally:
        with _counters_lock:
            _active_conversions -= 1
        if os.path.exists(tmp_in):
            try: os.remove(tmp_in)
            except Exception: pass
        _log_event(
            event_type="convert_sync",
            esito=esito,
            duration_sec=_time.time() - t0,
            file_size_mb=file_mb,
            formato=ext.lstrip("."),
            request=request,
            extra=extra,
        )


# ── Conversione asincrona (job-based) ─────────────────────────────────────────

@app.post("/convert-from-verify")
async def convert_from_verify(request: Request):
    """Avvia conversione riusando il file già caricato durante la verifica."""
    # ── Quota check ──
    from core.auth import get_email_by_token, get_conversions_used, FREE_CONVERSIONS_MAX
    _auth_email = None
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        _auth_email = get_email_by_token(authorization[7:])
    if _auth_email:
        # Controlla piano Stripe — Pro/Lifetime saltano la quota
        _user_plan = _get_user_plan(_auth_email)
        if _user_plan == "free" and get_conversions_used(_auth_email) >= FREE_CONVERSIONS_MAX:
            return JSONResponse(
                {"error": "Hai esaurito le 2 conversioni gratuite. Abbonati a Pro per conversioni illimitate."},
                status_code=403,
            )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Body JSON non valido"}, status_code=400)

    verify_job_id = body.get("verify_job_id", "")
    format = body.get("format", "mp3")
    if format not in ("mp3", "m4a"):
        format = "mp3"

    # Recupera il file dalla verifica
    with _jobs_lock:
        verify_job = _jobs.get(verify_job_id)
    if not verify_job:
        return JSONResponse({"error": "Job di verifica non trovato"}, status_code=404)

    tmp_in = verify_job.get("_tmp_path", "")
    if not tmp_in or not os.path.exists(tmp_in):
        return JSONResponse({"error": "File di verifica non più disponibile — ricarica il file"}, status_code=410)

    from core.converter import _get_duration
    duration_sec = _get_duration(tmp_in) or 0.0
    sox_timeout = max(120, int(duration_sec * 3))
    file_mb = os.path.getsize(tmp_in) / 1_000_000

    job_id = str(uuid.uuid4())
    ext = Path(tmp_in).suffix.lower()
    tmp_out = str(TEMP_DIR / f"{job_id}_432.{format}")
    filename = verify_job.get("_formato", "audio") + ext

    _save_job(job_id, {"status": "uploading", "progress": 0, "_auth_email": _auth_email})
    print(f"[convert/from-verify] job={job_id}  verify_job={verify_job_id}  size={file_mb:.1f}MB  duration={duration_sec:.1f}s  email={_auth_email}", flush=True)

    t = threading.Thread(target=_run_conversion,
                         args=(job_id, tmp_in, tmp_out, format, filename, file_mb, duration_sec, sox_timeout, False),
                         daemon=True)
    t.start()

    return JSONResponse({"job_id": job_id})

@app.post("/convert")
async def convert_start(request: Request, file: UploadFile = File(...), format: str = "mp3"):
    # ── Quota check ──
    from core.auth import get_email_by_token, get_conversions_used, increment_conversions, FREE_CONVERSIONS_MAX
    _auth_email = None
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        _auth_email = get_email_by_token(authorization[7:])
    if _auth_email:
        _user_plan = _get_user_plan(_auth_email)
        if _user_plan == "free" and get_conversions_used(_auth_email) >= FREE_CONVERSIONS_MAX:
            return JSONResponse(
                {"error": "Hai esaurito le 2 conversioni gratuite. Abbonati a Pro per conversioni illimitate."},
                status_code=403,
            )

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Formato non supportato: {ext}")
    if format not in ["mp3", "m4a"]:
        format = "mp3"

    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(413, "File troppo grande")
    file_mb = len(content) / 1_000_000

    job_id  = str(uuid.uuid4())
    tmp_in  = str(TEMP_DIR / f"{job_id}_in{ext}")
    tmp_out = str(TEMP_DIR / f"{job_id}_432.{format}")
    with open(tmp_in, "wb") as fh:
        fh.write(content)

    from core.converter import _get_duration
    duration_sec = _get_duration(tmp_in) or 0.0
    sox_timeout  = max(120, int(duration_sec * 3))

    _save_job(job_id, {"status": "uploading", "progress": 0, "_auth_email": _auth_email})
    print(f"[convert/async] job={job_id}  file={file.filename}  size={file_mb:.1f}MB  duration={duration_sec:.1f}s", flush=True)

    # Lancia la conversione in un thread background
    t = threading.Thread(target=_run_conversion,
                         args=(job_id, tmp_in, tmp_out, format, file.filename, file_mb, duration_sec, sox_timeout),
                         daemon=True)
    t.start()

    return JSONResponse({"job_id": job_id})

@app.get("/convert/status/{job_id}")
def convert_status(job_id: str):
    job = _load_job(job_id)
    if not job:
        raise HTTPException(404, "Job non trovato")
    # Non esporre output_path al client
    safe = {k: v for k, v in job.items() if k != "output_path"}
    return JSONResponse(safe)

@app.get("/convert/download/{job_id}")
def convert_download(job_id: str):
    job = _load_job(job_id)
    if not job:
        raise HTTPException(404, "Job non trovato")
    if job.get("status") != "done" or job.get("already_432"):
        raise HTTPException(400, "File non disponibile")
    output_path = job.get("output_path", "")
    if not output_path or not os.path.exists(output_path):
        raise HTTPException(404, "File di output non trovato")

    fmt  = job.get("format", "mp3")
    stem = Path(job.get("filename", "rephase")).stem
    media = "audio/mpeg" if fmt == "mp3" else "audio/mp4"

    # Cleanup job + file dopo 60s
    def _cleanup_job():
        import time; time.sleep(60)
        if os.path.exists(output_path):
            try: os.remove(output_path)
            except Exception: pass
        jp = _job_path(job_id)
        if jp.exists():
            try: jp.unlink()
            except Exception: pass
    threading.Thread(target=_cleanup_job, daemon=True).start()

    return FileResponse(path=output_path, filename=f"{stem}_432.{fmt}", media_type=media)

import asyncio
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import secrets

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/app", response_class=HTMLResponse)
async def frontend():
    with open("static/index.html") as f:
        return f.read()

# ── Legal pages ──────────────────────────────────────────────────────────────

@app.get("/privacy", response_class=HTMLResponse)
async def privacy_it():
    with open("static/privacy.html") as f:
        return f.read()

@app.get("/privacy/en", response_class=HTMLResponse)
async def privacy_en():
    with open("static/privacy_en.html") as f:
        return f.read()

@app.get("/terms", response_class=HTMLResponse)
async def terms_it():
    with open("static/terms.html") as f:
        return f.read()

@app.get("/terms/en", response_class=HTMLResponse)
async def terms_en():
    with open("static/terms_en.html") as f:
        return f.read()

@app.get("/termini", response_class=HTMLResponse)
async def termini():
    with open("static/termini.html") as f:
        return f.read()

# ── Admin ─────────────────────────────────────────────────────────────────────

_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
_FIXED_COSTS    = float(os.environ.get("ADMIN_FIXED_COSTS_CHF", "0"))

def _check_admin(authorization: str = Header(default=None)):
    if not _ADMIN_PASSWORD:
        raise HTTPException(503, "ADMIN_PASSWORD not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Unauthorized")
    if not secrets.compare_digest(authorization[7:].encode(), _ADMIN_PASSWORD.encode()):
        raise HTTPException(401, "Wrong password")

# ── Polling-based FFT analysis (replaces WebSocket) ───────────────────────────

@app.post("/analyze/start")
async def analyze_start(request: Request, file: UploadFile = File(...), full_analysis: str = Form(default="0")):
    global _active_analyses
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Formato non supportato: {ext}")
    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(413, "File troppo grande")
    file_mb = len(content) / 1_000_000

    # Incrementa verifiche_totali per abbonati (no-op se non in tabella) + log operazione
    authorization = request.headers.get("authorization", "")
    _verify_email = None
    if authorization.startswith("Bearer "):
        from core.auth import get_email_by_token
        _verify_email = get_email_by_token(authorization[7:])
        if _verify_email:
            try:
                increment_verifica_abbonato(_verify_email)
            except Exception as ex:
                print(f"[abbonati] increment_verifica errore: {ex}", flush=True)
    try:
        _v_plan = _get_user_plan(_verify_email) if _verify_email else "free"
        _log_operazione(_verify_email, _v_plan, "verifica", request)
    except Exception as ex:
        print(f"[log_operazioni] analyze/start errore: {ex}", flush=True)

    uid = str(uuid.uuid4())
    tmp = str(TEMP_DIR / f"{uid}{ext}")
    with open(tmp, "wb") as fh:
        fh.write(content)

    job_id  = str(uuid.uuid4())
    is_full = full_analysis in ("1", "true", "True")

    with _jobs_lock:
        _jobs[job_id] = {
            "status":     "running",
            "windows":    [],
            "result":     None,
            "error":      None,
            "created_at": _time.time(),
            # vigile metadata
            "_file_mb":   file_mb,
            "_formato":   ext.lstrip("."),
            "_t0":        _time.time(),
            "_request_ip": request.client.host if request.client else "",
            "_ua":         (request.headers.get("user-agent", "") or "")[:80],
        }

    with _counters_lock:
        _active_analyses += 1

    def _run():
        global _active_analyses
        import tempfile, os as _os
        from core.converter import (
            _load_as_wav, _load_as_wav_sampled, _is_large_file,
            _read_wav_samples, _measure_a4_streaming,
        )
        tmp_wav = tempfile.mktemp(suffix=".wav")
        esito   = "ok"
        try:
            if is_full:
                _load_as_wav(tmp, tmp_wav, channels=1, max_seconds=None)
            elif _is_large_file(tmp):
                _load_as_wav_sampled(tmp, tmp_wav, channels=1)
                with _jobs_lock:
                    _jobs[job_id]["sampled"] = True
            else:
                _load_as_wav(tmp, tmp_wav, channels=1, max_seconds=90)
            samples, sr = _read_wav_samples(tmp_wav)
            for msg in _measure_a4_streaming(samples, sr):
                with _jobs_lock:
                    if msg.get("type") == "window":
                        _jobs[job_id]["windows"].append(msg)
                    elif msg.get("type") == "done":
                        _jobs[job_id]["result"] = msg["result"]
                        _jobs[job_id]["status"] = "done"
                    elif msg.get("type") == "error":
                        _jobs[job_id]["status"] = "error"
                        _jobs[job_id]["error"]  = msg.get("error", "Errore sconosciuto")
                        esito = "error"
        except Exception as e:
            esito = "error"
            with _jobs_lock:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"]  = str(e)
        finally:
            if _os.path.exists(tmp_wav): _os.remove(tmp_wav)
            # Conserva il file originale (tmp) per eventuale conversione successiva.
            # Verrà cancellato dopo 5 minuti se non usato.
            def _delayed_cleanup():
                import time; time.sleep(300)
                if _os.path.exists(tmp): _os.remove(tmp)
            threading.Thread(target=_delayed_cleanup, daemon=True).start()
            with _jobs_lock:
                _jobs[job_id]["_tmp_path"] = tmp
            with _jobs_lock:
                if _jobs[job_id]["status"] == "running":
                    _jobs[job_id]["status"] = "error"
                    _jobs[job_id]["error"]  = "Analisi interrotta"
                    esito = "error"
                meta = _jobs[job_id]

            with _counters_lock:
                _active_analyses -= 1

            # Log vigile dal thread
            duration = _time.time() - meta["_t0"]

            class _FakeRequest:
                class client:
                    host = meta["_request_ip"]
                class headers:
                    @staticmethod
                    def get(k, d=""): return meta["_ua"] if k == "user-agent" else d

            _log_event(
                event_type="analyze",
                esito=esito,
                duration_sec=duration,
                file_size_mb=meta["_file_mb"],
                formato=meta["_formato"],
                request=_FakeRequest(),
                extra={"full": is_full},
            )

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"job_id": job_id})


@app.get("/analyze/status/{job_id}")
async def analyze_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job non trovato o scaduto")
        return JSONResponse({
            "status":  job["status"],
            "windows": job["windows"],
            "result":  job["result"],
            "error":   job["error"],
            "sampled": job.get("sampled", False),
        })


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    with open("static/admin.html") as f:
        return f.read()

@app.get("/admin/metrics")
async def admin_metrics(_: None = Depends(_check_admin)):
    from core.stripe_metrics import get_metrics
    from core.costs import load_costs, total_monthly_chf
    costs_data = load_costs()
    fixed = total_monthly_chf(costs_data) or _FIXED_COSTS
    data  = get_metrics(fixed_costs_chf=fixed, costs_data=costs_data)
    if "error" in data:
        raise HTTPException(502, data["error"])
    return JSONResponse(data)

@app.get("/admin/costs")
async def get_costs(_: None = Depends(_check_admin)):
    from core.costs import load_costs
    return JSONResponse(load_costs())

@app.post("/admin/costs")
async def post_costs(payload: dict, _: None = Depends(_check_admin)):
    from core.costs import save_costs, load_costs
    import uuid as _uuid
    costs_data = load_costs()
    if "launch_date" in payload:
        costs_data["launch_date"] = payload["launch_date"]
    if "items" in payload:
        for item in payload["items"]:
            if not item.get("id"):
                item["id"] = str(_uuid.uuid4())[:8]
        costs_data["items"] = payload["items"]
    if "phases" in payload:
        costs_data["phases"] = payload["phases"]
    save_costs(costs_data)
    from core.stripe_metrics import _cache
    _cache["ts"] = 0.0
    return JSONResponse({"ok": True})

# ══════════════════════════════════════════════════════════════════════════════
# 🚦 VIGILE URBANO — endpoint admin/log
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/admin/log")
async def admin_log(last: int = 100, _: None = Depends(_check_admin)):
    """
    Restituisce gli ultimi N eventi con statistiche aggregate.
    Chiamata: GET /admin/log?last=100
              Authorization: Bearer <ADMIN_PASSWORD>
    """
    events = list(_event_log)[-last:]

    today_str = datetime.now(timezone.utc).date().isoformat()
    oggi = [e for e in list(_event_log) if e["ts"].startswith(today_str)]

    # Statistiche per tipo
    def _stats(tipo):
        subset = [e for e in oggi if e["type"] == tipo]
        ok     = [e for e in subset if e["esito"] == "ok"]
        durate = [e["duration_sec"] for e in ok]
        return {
            "totale":       len(subset),
            "ok":           len(ok),
            "errori":       len([e for e in subset if e["esito"] == "error"]),
            "already_432":  len([e for e in subset if e["esito"] == "already_432"]),
            "durata_media": round(sum(durate) / len(durate), 1) if durate else 0,
            "durata_max":   round(max(durate), 1) if durate else 0,
        }

    try:
        mem_pct = psutil.virtual_memory().percent
        cpu_pct = psutil.cpu_percent(interval=None)
    except Exception:
        mem_pct = cpu_pct = -1.0

    with _counters_lock:
        active_now = {
            "conversions": _active_conversions,
            "verifies":    _active_verifies,
            "analyses":    _active_analyses,
        }

    return JSONResponse({
        "active_now": active_now,
        "mem_pct":    mem_pct,
        "cpu_pct":    cpu_pct,
        "oggi": {
            "verify":        _stats("verify"),
            "convert":       _stats("convert"),
            "convert_sync":  _stats("convert_sync"),
            "analyze":       _stats("analyze"),
        },
        "events": events,
    })


# ── Admin report log_operazioni ───────────────────────────────────────────────

def _check_admin_token_header(request: Request):
    """Verifica header X-Admin-Token contro env var ADMIN_TOKEN."""
    expected = os.environ.get("ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(503, "ADMIN_TOKEN non configurato sul server")
    provided = request.headers.get("x-admin-token", "")
    if provided != expected:
        raise HTTPException(401, "Token admin non valido")

@app.get("/admin/report")
async def admin_report(request: Request):
    """Report aggregato del log operazioni. Richiede header X-Admin-Token."""
    _check_admin_token_header(request)
    with _blacklist_lock:
        conn = sqlite3.connect(_BLACKLIST_DB_PATH)
        try:
            # Totali
            tot_v = conn.execute("SELECT COUNT(*) FROM log_operazioni WHERE tipo='verifica'").fetchone()[0]
            tot_c = conn.execute("SELECT COUNT(*) FROM log_operazioni WHERE tipo='conversione'").fetchone()[0]

            # Per mese (YYYY-MM)
            rows_mese = conn.execute("""
                SELECT substr(timestamp, 1, 7) AS mese,
                       SUM(CASE WHEN tipo='verifica' THEN 1 ELSE 0 END) AS verifiche,
                       SUM(CASE WHEN tipo='conversione' THEN 1 ELSE 0 END) AS conversioni
                FROM log_operazioni
                WHERE timestamp IS NOT NULL AND timestamp != ''
                GROUP BY mese
                ORDER BY mese DESC
            """).fetchall()
            per_mese = [
                {"mese": r[0], "verifiche": r[1], "conversioni": r[2]}
                for r in rows_mese
            ]

            # Per piano
            rows_piano = conn.execute("""
                SELECT piano, COUNT(*) FROM log_operazioni GROUP BY piano
            """).fetchall()
            per_piano = {"free": 0, "pro_monthly": 0, "pro_annual": 0, "lifetime": 0}
            for piano, cnt in rows_piano:
                key = piano or "free"
                per_piano[key] = per_piano.get(key, 0) + cnt
        finally:
            conn.close()

    return JSONResponse({
        "totale_verifiche":   tot_v,
        "totale_conversioni": tot_c,
        "per_mese":           per_mese,
        "per_piano":          per_piano,
    })

@app.get("/admin/report/csv")
async def admin_report_csv(request: Request):
    """Esporta CSV completo del log_operazioni (no email in chiaro). Richiede X-Admin-Token."""
    _check_admin_token_header(request)
    import io, csv
    from fastapi.responses import StreamingResponse
    with _blacklist_lock:
        conn = sqlite3.connect(_BLACKLIST_DB_PATH)
        try:
            rows = conn.execute("""
                SELECT id, username, email_hash, piano, tipo, timestamp, ip_hash
                FROM log_operazioni
                ORDER BY id ASC
            """).fetchall()
        finally:
            conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "username", "email_hash", "piano", "tipo", "timestamp", "ip_hash"])
    for row in rows:
        writer.writerow(row)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=rephase_log_operazioni.csv"},
    )


# ── Certification / Blockchain ─────────────────────────────────────────────

@app.post("/certify")
def certify_report(payload: dict):
    """Compute SHA-256 of the report JSON and anchor it on Bitcoin via OriginStamp."""
    import hashlib, json as _j, urllib.request, urllib.error

    report_str  = _j.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    report_hash = hashlib.sha256(report_str.encode("utf-8")).hexdigest()

    api_key = os.environ.get("ORIGINSTAMP_API_KEY", "")
    if not api_key:
        return JSONResponse({"report_hash": report_hash, "originstamp": None,
                             "warning": "ORIGINSTAMP_API_KEY non configurato — hash calcolato localmente."})

    body = _j.dumps({
        "hash":    report_hash,
        "comment": f"Rephase: {payload.get('file', {}).get('name', 'audio')}",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.originstamp.com/v4/timestamp/create",
        data=body,
        headers={"Authorization": f"Token {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            os_data = _j.loads(r.read())
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"OriginStamp HTTP {e.code}: {e.reason}")
    except Exception as e:
        raise HTTPException(502, f"OriginStamp error: {e}")

    return JSONResponse({"report_hash": report_hash, "originstamp": os_data})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True, timeout_keep_alive=600)


# ── Stripe Checkout ────────────────────────────────────────────────────────

# ── Auth OTP ──────────────────────────────────────────────────────────────────

@app.post("/validate-email")
async def validate_email_endpoint(request: Request):
    """Valida un indirizzo email — blocca servizi di email temporanea."""
    from core.email_validator import validate_email
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"valid": False, "error": "Body JSON non valido"}, status_code=400)
    email = body.get("email", "")
    result = validate_email(email)
    status = 200 if result["valid"] else 400
    return JSONResponse(result, status_code=status)

@app.post("/auth/send-otp")
async def send_otp_endpoint(request: Request):
    """Invia codice OTP a 6 cifre via email (Resend)."""
    from core.auth import generate_otp
    from core.email_validator import validate_email
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Body JSON non valido"}, status_code=400)
    email = body.get("email", "")
    if not email:
        return JSONResponse({"success": False, "error": "Email richiesta"}, status_code=400)
    validation = validate_email(email)
    if not validation["valid"]:
        return JSONResponse({"success": False, "error": validation["error"]}, status_code=400)

    # ── Blacklist Free SQLite: lookup O(1) ──
    if is_free_exhausted(email):
        # Step 1: lookup DB locale (zero chiamate Stripe)
        db_plan, db_scadenza, _v, _c = _get_plan_from_db(email)
        if db_plan and db_plan != "free":
            print(f"[auth] email in blacklist ma DB locale piano={db_plan} → sbloccata automaticamente (no Stripe): {email}", flush=True)
            unmark_free_exhausted(email)
        else:
            # Step 2: fallback Stripe (DB locale vuoto o piano free)
            try:
                stripe_plan = _get_user_plan(email)
            except Exception as ex:
                print(f"[auth] _get_user_plan unreachable: {type(ex).__name__}: {ex} — non blocco l'utente per sicurezza", flush=True)
                stripe_plan = None  # Stripe irraggiungibile → non bloccare
            if stripe_plan and stripe_plan != "free":
                print(f"[auth] email in blacklist ma Stripe piano={stripe_plan} → sbloccata automaticamente: {email}", flush=True)
                unmark_free_exhausted(email)
            elif stripe_plan == "free":
                # Step 3: Stripe conferma free → blocca
                print(f"[auth/send-otp] BLOCCATO free_exhausted: {email} piano=free (DB+Stripe)", flush=True)
                return JSONResponse({
                    "success": False,
                    "error": "free_exhausted",
                    "message": "Hai già utilizzato le 2 conversioni gratuite. Scegli un piano per continuare."
                }, status_code=403)
            else:
                # Stripe irraggiungibile e DB vuoto → safe-mode: lascia procedere (non bloccare paganti per outage)
                print(f"[auth] Stripe irraggiungibile e DB vuoto → procedo con OTP (safe-mode): {email}", flush=True)

    result = generate_otp(email)
    status = 200 if result["success"] else 400
    return JSONResponse(result, status_code=status)

@app.post("/auth/verify-otp")
async def verify_otp_endpoint(request: Request):
    """Verifica codice OTP e crea sessione."""
    from core.auth import verify_otp
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Body JSON non valido"}, status_code=400)
    email = body.get("email", "")
    code  = body.get("code", "")
    if not email or not code:
        return JSONResponse({"success": False, "error": "Email e codice richiesti"}, status_code=400)
    result = verify_otp(email, code)
    status = 200 if result["success"] else 400
    return JSONResponse(result, status_code=status)

@app.get("/auth/conversions")
async def auth_conversions(request: Request):
    """Ritorna le conversioni usate/rimanenti per l'utente autenticato."""
    from core.auth import get_email_by_token, get_conversions_used, FREE_CONVERSIONS_MAX
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return JSONResponse({"error": "Token mancante"}, status_code=401)
    token = authorization[7:]
    email = get_email_by_token(token)
    if not email:
        return JSONResponse({"error": "Sessione non valida"}, status_code=401)
    used = get_conversions_used(email)
    plan, sub_end = _get_user_plan_details(email)
    username = get_username_by_email(email)

    if plan != "free":
        return JSONResponse({
            "email": email,
            "username": username,
            "plan": plan,
            "subscription_end": sub_end,  # ISO UTC, solo per pro_monthly/pro_annual
            "conversions_used": used,
            "conversions_max": -1,
            "conversions_remaining": -1,
        })
    return JSONResponse({
        "email": email,
        "username": username,
        "plan": plan,
        "subscription_end": None,
        "conversions_used": used,
        "conversions_max": FREE_CONVERSIONS_MAX,
        "conversions_remaining": max(0, FREE_CONVERSIONS_MAX - used),
    })

# ── Stripe Checkout ───────────────────────────────────────────────────────────

@app.post("/create-checkout-session")
async def create_checkout_session(request: Request):
    """Crea una sessione Stripe Checkout per abbonamento mensile o annuale."""
    print("[checkout] route chiamata", flush=True)
    try:
        body = await request.json()
        print(f"[checkout] body={body}", flush=True)
    except Exception as e:
        print(f"[checkout] body parse error: {e}", flush=True)
        return JSONResponse({"error": "Body JSON non valido"}, status_code=400)

    # Recupera email utente dalla sessione (se autenticato)
    from core.auth import get_email_by_token
    customer_email = None
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        customer_email = get_email_by_token(authorization[7:])
    print(f"[checkout] customer_email={customer_email}", flush=True)

    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    print(f"[checkout] stripe_key={'SET' if stripe.api_key else 'MISSING'}", flush=True)
    if not stripe.api_key:
        return JSONResponse({"error": "STRIPE_SECRET_KEY non configurata"}, status_code=500)

    plan = body.get("plan", "monthly")
    auto_renew = body.get("auto_renew", True)
    if not isinstance(auto_renew, bool):
        auto_renew = True
    print(f"[checkout] plan={plan} auto_renew={auto_renew}", flush=True)
    if plan == "annual":
        price_id = os.environ.get("STRIPE_PRICE_ANNUAL", "")
    elif plan == "lifetime":
        price_id = os.environ.get("STRIPE_PRICE_LIFETIME", "")
    else:
        price_id = os.environ.get("STRIPE_PRICE_MONTHLY", "")
    print(f"[checkout] plan={plan} price_id={'SET('+price_id[:12]+')' if price_id else 'MISSING'}", flush=True)

    if not price_id:
        return JSONResponse({"error": f"Price ID non configurato per piano: {plan}"}, status_code=400)

    base_url = os.environ.get("BASE_URL", "https://getrephase.com")
    checkout_mode = "payment" if plan == "lifetime" else "subscription"

    try:
        checkout_params = dict(
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode=checkout_mode,
            success_url=f"{base_url}/app?payment=success",
            cancel_url=f"{base_url}/app?payment=cancelled",
        )
        if customer_email:
            checkout_params["customer_email"] = customer_email
        # Per subscription: se auto_renew=False, crea con cancel_at_period_end
        # → Stripe addebita il primo periodo, poi cancella automaticamente alla scadenza
        if checkout_mode == "subscription" and not auto_renew:
            checkout_params["subscription_data"] = {"cancel_at_period_end": True}
            print(f"[checkout] subscription con cancel_at_period_end=True (no rinnovo)", flush=True)
        session = stripe.checkout.Session.create(**checkout_params)
        print(f"[checkout] session creata: {session.id}", flush=True)
        return JSONResponse({"url": session.url})
    except Exception as e:
        print(f"[checkout] ERRORE stripe: {type(e).__name__}: {e}", flush=True)
        return JSONResponse({"error": f"Stripe error: {str(e)}"}, status_code=502)

# ── Stripe Webhook ───────────────────────────────────────────────────────────

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """Gestisce eventi Stripe (checkout completato, pagamento riuscito, cancellazione)."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    if not webhook_secret:
        print("[webhook] STRIPE_WEBHOOK_SECRET non configurata, skip verifica firma", flush=True)
        try:
            event = stripe.Event.construct_from(
                stripe.util.convert_to_stripe_object(
                    __import__("json").loads(payload)
                ),
                stripe.api_key,
            )
        except Exception as e:
            print(f"[webhook] parse error: {e}", flush=True)
            return JSONResponse({"error": "Payload non valido"}, status_code=400)
    else:
        try:
            stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        except stripe.error.SignatureVerificationError:
            print("[webhook] Firma non valida", flush=True)
            return JSONResponse({"error": "Firma non valida"}, status_code=400)
        except Exception as e:
            print(f"[webhook] errore: {e}", flush=True)
            return JSONResponse({"error": str(e)}, status_code=400)

    event_type = event.get("type", "")
    print(f"[webhook] evento ricevuto: {event_type}", flush=True)

    if event_type == "checkout.session.completed":
        session_obj = event["data"]["object"]
        customer_email = session_obj.get("customer_email") or session_obj.get("customer_details", {}).get("email")
        payment_status = session_obj.get("payment_status")
        print(f"[webhook] checkout completato — email={customer_email} payment_status={payment_status}", flush=True)
        if customer_email and payment_status == "paid":
            _vigile.info(f"{datetime.now(timezone.utc).isoformat()} | WEBHOOK | checkout.completed | email={customer_email}")
            # Sblocca dalla blacklist Free
            unmark_free_exhausted(customer_email)
            # Lifetime: salva subito in tabella abbonati (no invoice ricorrente)
            if session_obj.get("mode") == "payment":
                amount_total = (session_obj.get("amount_total") or 0) / 100
                upsert_abbonato(
                    email=customer_email,
                    piano="lifetime",
                    data_scadenza=None,  # accesso a vita
                    importo_chf=amount_total,
                    stripe_event_id=event.get("id"),
                )

    elif event_type in ("customer.subscription.created", "customer.subscription.updated"):
        sub = event["data"]["object"]
        customer_id = sub.get("customer")
        status = sub.get("status")
        print(f"[webhook] subscription {event_type} — customer={customer_id} status={status}", flush=True)
        _vigile.info(f"{datetime.now(timezone.utc).isoformat()} | WEBHOOK | {event_type} | customer={customer_id} status={status}")
        # Determina email e piano dalla subscription, poi salva
        try:
            stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
            cust = stripe.Customer.retrieve(customer_id) if customer_id else None
            sub_email = (cust.email if cust else None)
            price_id = ""
            try:
                price_id = sub["items"]["data"][0]["price"]["id"]
            except (KeyError, IndexError, TypeError):
                pass
            price_annual  = os.environ.get("STRIPE_PRICE_ANNUAL", "")
            piano = "pro_annual" if price_id == price_annual else "pro_monthly"
            # data_scadenza = current_period_end (Unix timestamp → ISO)
            scadenza_unix = sub.get("current_period_end")
            scadenza_iso = datetime.fromtimestamp(scadenza_unix, tz=timezone.utc).isoformat() if scadenza_unix else None
            # importo dal price.unit_amount
            importo = None
            try:
                importo = sub["items"]["data"][0]["price"]["unit_amount"] / 100
            except (KeyError, IndexError, TypeError):
                pass
            if sub_email and status == "active":
                upsert_abbonato(
                    email=sub_email,
                    piano=piano,
                    data_scadenza=scadenza_iso,
                    importo_chf=importo,
                    stripe_event_id=event.get("id"),
                )
                unmark_free_exhausted(sub_email)
        except Exception as ex:
            print(f"[webhook] errore upsert abbonato (sub.{event_type}): {type(ex).__name__}: {ex}", flush=True)

    elif event_type == "customer.subscription.deleted":
        sub = event["data"]["object"]
        customer_id = sub.get("customer")
        print(f"[webhook] subscription cancellata — customer={customer_id}", flush=True)
        _vigile.info(f"{datetime.now(timezone.utc).isoformat()} | WEBHOOK | subscription.deleted | customer={customer_id}")

    elif event_type == "invoice.payment_succeeded":
        invoice = event["data"]["object"]
        customer_email = invoice.get("customer_email")
        amount = invoice.get("amount_paid", 0) / 100
        print(f"[webhook] pagamento riuscito — email={customer_email} amount={amount}", flush=True)
        _vigile.info(f"{datetime.now(timezone.utc).isoformat()} | WEBHOOK | invoice.paid | email={customer_email} amount={amount}")
        # Salva/aggiorna abbonato in tabella
        try:
            sub_id = invoice.get("subscription")
            piano = "pro_monthly"
            scadenza_iso = None
            if sub_id:
                stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
                sub_obj = stripe.Subscription.retrieve(sub_id)
                price_id = ""
                try:
                    price_id = sub_obj["items"]["data"][0]["price"]["id"]
                except (KeyError, IndexError, TypeError):
                    pass
                price_annual = os.environ.get("STRIPE_PRICE_ANNUAL", "")
                piano = "pro_annual" if price_id == price_annual else "pro_monthly"
                scadenza_unix = sub_obj.get("current_period_end")
                if scadenza_unix:
                    scadenza_iso = datetime.fromtimestamp(scadenza_unix, tz=timezone.utc).isoformat()
            if customer_email:
                upsert_abbonato(
                    email=customer_email,
                    piano=piano,
                    data_scadenza=scadenza_iso,
                    importo_chf=amount,
                    stripe_event_id=event.get("id"),
                )
                unmark_free_exhausted(customer_email)
        except Exception as ex:
            print(f"[webhook] errore upsert abbonato (invoice.paid): {type(ex).__name__}: {ex}", flush=True)

    elif event_type == "invoice.payment_failed":
        invoice = event["data"]["object"]
        customer_email = invoice.get("customer_email")
        print(f"[webhook] pagamento fallito — email={customer_email}", flush=True)
        _vigile.info(f"{datetime.now(timezone.utc).isoformat()} | WEBHOOK | invoice.failed | email={customer_email}")

    return JSONResponse({"received": True})

