# MailForge — Email Automation Studio

A Python (FastAPI) + SQLite + APScheduler email automation system with a modern web interface.
Upload an Excel sheet of recipients, compose dynamic templates (HTML or Plain Text with `{{Column}}` placeholders), and schedule deliveries (Immediate, Once, Daily, Weekly, Monthly, Yearly).

## Features
- **Excel Import**: Drag & drop `.xlsx`/`.csv`, auto-detects email column, shows 10-row preview and `{{variable}}` tags.
- **Dynamic Templates**: Toggle **Rich Text/HTML** ↔ **Plain Text**. Insert `{{Name}}`, `{{Company}}`, etc., case-insensitive per-recipient rendering + live preview.
- **Scheduler**: APScheduler cron/date triggers — Immediate, Once (`YYYY-MM-DDTHH:MM`), Daily `09:00`, Weekly (weekday + time), Monthly (day + time), Yearly (month/day/time).
- **SMTP**: Host/port/user/pass/sender + TLS/SSL, with **Send Test Email** verification.
- **Dashboard & Logs**: Campaign list (run now / preview / delete), stats, and full audit logs (SUCCESS/FAILED/INFO).

## Tech Stack
Backend: FastAPI, uvicorn, pandas, openpyxl, APScheduler, SQLite. Frontend: Single-page vanilla JS + Tailwind CDN + Font Awesome served from FastAPI `static/`.

## Quick Start
```bash
cd /Users/alpdurmus/email-automation-system
chmod +x run.sh
./run.sh
# open http://127.0.0.1:8000
# API docs http://127.0.0.1:8000/docs
```

Manual:
```bash
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
```

## Sample Data
`sample_recipients.xlsx` (also in `static/`) contains 4 rows: Email, Name, Company, Amount, Month — ready to drag into Step 1.

## Typical Flow
1. **Settings** → configure SMTP (e.g., Gmail `smtp.gmail.com:587 TLS` with App Password) → Send Test Email.
2. **New Campaign** → Step 1: drop Excel → select Email column → set title.
3. Step 2: compose Subject/Body — click tags to insert `{{Name}}` — toggle HTML/Plain — Preview.
4. Step 3: choose schedule type → Create & Schedule.
5. **Campaigns** → Run Now / Preview. **Logs** → verify per-recipient status. **Dashboard** → overview.

## API
- `POST /api/upload-excel` (multipart file)
- `GET/POST /api/smtp-settings`, `POST /api/smtp-test`
- `POST/GET /api/campaigns`, `POST /api/campaigns/{id}/run-now`, `POST /api/campaigns/{id}/preview`
- `GET /api/logs`, `GET /api/stats`
- Static UI at `/`, file at `/static`.

## Notes
- Scheduler runs in UTC (stored as such). Use server logs for troubleshooting.
- Campaigns `immediate`/`once` mark `completed` after run; recurring stays `scheduled`.
- SQLite DB: `automation.db` in project root.
