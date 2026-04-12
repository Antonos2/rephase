# Rephase — Stato Progetto

> Ultimo aggiornamento: 2026-04-12

## Architettura

- **Backend**: FastAPI (Python 3.9+) su Render — pitch engine Rubber Band R3 / ffmpeg librubberband / SoX
- **Frontend**: SPA statica in `static/index.html` — upload, analisi FFT, conversione, certificati
- **Extension**: Chrome extension separata (`~/Desktop/rephase-extension`) — widget YIN pitch detection, classificazione A432/A440
- **MCP Server**: `scripts/mcp_server.py` — bridge di coordinamento Claude Chat ↔ Claude Code ↔ Flow

## Deploy

- **Piattaforma**: Render (Web Service, Docker)
- **URL**: https://getrephase.com
- **Auto-deploy**: push su `main` → build + deploy automatico (2-5 min)
- **Coming Soon**: middleware attivo — le pagine HTML pubbliche (/, /app, /privacy, /terms, /admin) mostrano "Stiamo per arrivare"
- **Endpoint API**: tutti attivi (/health, /verify, /convert, /analyze, /certify, /create-checkout-session)

## Stripe

- **Stato**: LIVE (non test mode)
- **Tier**: Free (2 conversioni), Pro Monthly (CHF 4.95), Pro Annual (CHF 49), Lifetime (CHF 199)
- **Webhook**: configurato su Render, endpoint `/webhook`
- **OTP**: autenticazione via email con Resend, reinvio con countdown 15s

## Vigile Urbano

- Logger interno su `rephase_events.log` + deque in memoria (5000 eventi)
- Monitora: verify, convert, convert_sync, analyze
- Dashboard admin su /admin con metriche e costi

## DNS

- **Stato**: IN ATTESA
- **Registrar**: Infomaniak (dominio `rephase.app`)
- **Configurazione necessaria**: puntare A/CNAME al servizio Render
- **Dominio attuale**: getrephase.com (funzionante)

## Coming Soon

- **Middleware**: `ComingSoonMiddleware` in `main.py`
- **Route bloccate**: `/`, `/app`, `/privacy`, `/privacy/en`, `/terms`, `/terms/en`, `/admin`
- **Per disattivare**: rimuovere `app.add_middleware(ComingSoonMiddleware)` dalla riga ~162 di main.py
- **Pagina**: logo Rephase + "Stiamo per arrivare" su sfondo scuro

## Extension Chrome

- **Repo**: `~/Desktop/rephase-extension`
- **Stato**: in sviluppo attivo — widget con 3 stati (micro/pill/expanded)
- **Features**: YIN pitch detection, classificazione A432/A440 con histogram voting, conversione playbackRate, fascia arancione ±8-14 cents, stop campionamento durante ad YouTube
- **Chrome Web Store**: non ancora pubblicata

## Regole operative

- **Modello**: sempre `claude-opus-4-6`
- **File proibito**: MAI leggere/aprire/modificare `REPHASE_credenziali_private`
- **Validazione**: `python3 -c "import py_compile; py_compile.compile('main.py', doraise=True)"` prima di committare
- **Push**: `git push origin main` dopo ogni commit significativo (Render deploya automaticamente)
