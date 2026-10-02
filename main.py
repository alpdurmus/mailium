from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
import os
import json
import sqlite3
import uuid
import shutil
import hashlib
import secrets
import re as _auth_re
from datetime import datetime, timedelta, timezone

from database import init_db, get_db
from excel_service import parse_excel_file
from email_service import send_single_email, render_template
from scheduler_service import schedule_campaign, execute_campaign, restore_schedules, scheduler, ensure_scheduler, get_smtp_config, log_event

app = FastAPI(title="Mailium - E-posta Otomasyon Sistemi")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Auth Helpers ----------
def _hash_password(password: str, salt_hex: str = None):
    if salt_hex is None:
        salt = secrets.token_bytes(16)
        salt_hex = salt.hex()
    else:
        salt = bytes.fromhex(salt_hex)
    # PBKDF2-HMAC-SHA256, 120k iterations
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 120000)
    return dk.hex(), salt_hex

def _verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    calc, _ = _hash_password(password, salt_hex)
    return secrets.compare_digest(calc, hash_hex)

def _create_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=7)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO auth_tokens (token, user_id, expires_at) VALUES (?,?,?)", (token, user_id, expires.isoformat()))
    conn.commit()
    conn.close()
    return token

def _get_user_by_token(token: str):
    if not token:
        return None
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM auth_tokens WHERE token=?", (token,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None
    try:
        exp = datetime.fromisoformat(row["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < datetime.now(timezone.utc):
            cur.execute("DELETE FROM auth_tokens WHERE token=?", (token,))
            conn.commit()
            conn.close()
            return None
    except Exception:
        pass
    cur.execute("SELECT id, email, name, created_at FROM users WHERE id=?", (row["user_id"],))
    u = cur.fetchone()
    conn.close()
    if not u:
        return None
    return dict(u)

def _extract_token(authorization: str = Header(None)):
    if not authorization:
        return None
    # Expected "Bearer <token>"
    if authorization.startswith("Bearer "):
        return authorization[7:].strip()
    return authorization.strip()

def get_current_user(authorization: str = Header(None)):
    token = _extract_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Oturum gerekli — lütfen giriş yapın")
    user = _get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Oturum süresi doldu veya geçersiz — tekrar giriş yapın")
    return user

def get_current_user_optional(authorization: str = Header(None)):
    token = _extract_token(authorization)
    if not token:
        return None
    return _get_user_by_token(token)

# Init DB and restore schedules on startup
@app.on_event("startup")
def on_startup():
    init_db()
    restore_schedules()
    # Cleanup expired tokens periodically
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM auth_tokens WHERE expires_at < ?", (datetime.now(timezone.utc).isoformat(),))
        conn.commit()
        conn.close()
    except Exception:
        pass

# ---------- Auth: Register / Login ----------
EMAIL_RE = _auth_re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

@app.post("/api/auth/register")
def auth_register(payload: dict):
    email = str(payload.get("email","")).strip().lower()
    password = str(payload.get("password",""))
    name = str(payload.get("name","")).strip()
    if not email or not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Geçerli bir e-posta girin")
    if not password or len(password) < 6:
        raise HTTPException(status_code=400, detail="Şifre en az 6 karakter olmalı")
    if not name:
        name = email.split("@")[0]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE email=?", (email,))
    if cur.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Bu e-posta zaten kayıtlı — giriş yapın")
    pw_hash, salt = _hash_password(password)
    cur.execute("INSERT INTO users (email, password_hash, salt, name) VALUES (?,?,?,?)", (email, pw_hash, salt, name))
    uid = cur.lastrowid
    conn.commit()
    conn.close()
    token = _create_token(uid)
    return {"ok": True, "token": token, "user": {"id": uid, "email": email, "name": name}, "message": "Kayıt başarılı"}

@app.post("/api/auth/login")
def auth_login(payload: dict):
    email = str(payload.get("email","")).strip().lower()
    password = str(payload.get("password",""))
    if not email or not password:
        raise HTTPException(status_code=400, detail="E-posta ve şifre gerekli")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE email=?", (email,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=401, detail="E-posta veya şifre hatalı")
    if not _verify_password(password, row["salt"], row["password_hash"]):
        raise HTTPException(status_code=401, detail="E-posta veya şifre hatalı")
    token = _create_token(row["id"])
    return {"ok": True, "token": token, "user": {"id": row["id"], "email": row["email"], "name": row["name"]}}

@app.post("/api/auth/logout")
def auth_logout(authorization: str = Header(None)):
    token = _extract_token(authorization)
    if token:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM auth_tokens WHERE token=?", (token,))
        conn.commit()
        conn.close()
    return {"ok": True, "message": "Çıkış yapıldı"}

@app.get("/api/auth/me")
def auth_me(user = Depends(get_current_user)):
    return {"ok": True, "user": user}

@app.put("/api/auth/profile")
def auth_update_profile(payload: dict, user = Depends(get_current_user)):
    name = str(payload.get("name","")).strip()
    if not name or len(name) < 2:
        raise HTTPException(status_code=400, detail="Ad en az 2 karakter olmalı")
    if len(name) > 60:
        raise HTTPException(status_code=400, detail="Ad çok uzun")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET name=? WHERE id=?", (name, user["id"]))
    conn.commit()
    cur.execute("SELECT id, email, name, created_at FROM users WHERE id=?", (user["id"],))
    updated = dict(cur.fetchone())
    conn.close()
    return {"ok": True, "user": updated, "message": "Profil güncellendi"}

@app.post("/api/auth/change-password")
def auth_change_password(payload: dict, user = Depends(get_current_user)):
    current = str(payload.get("current_password") or payload.get("old_password") or "")
    new_pw = str(payload.get("new_password") or "")
    if not current or not new_pw:
        raise HTTPException(status_code=400, detail="Mevcut ve yeni şifre gerekli")
    if len(new_pw) < 6:
        raise HTTPException(status_code=400, detail="Yeni şifre en az 6 karakter olmalı")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT password_hash, salt FROM users WHERE id=?", (user["id"],))
    row = cur.fetchone()
    if not row or not _verify_password(current, row["salt"], row["password_hash"]):
        conn.close()
        raise HTTPException(status_code=401, detail="Mevcut şifre hatalı")
    new_hash, new_salt = _hash_password(new_pw)
    cur.execute("UPDATE users SET password_hash=?, salt=? WHERE id=?", (new_hash, new_salt, user["id"]))
    # Invalidate other tokens except current
    try:
        cur.execute("DELETE FROM auth_tokens WHERE user_id=?", (user["id"],))
        # Re-create current token to keep session alive
        import secrets as _sec
        from datetime import timedelta, timezone, datetime as _dt
        token = _sec.token_urlsafe(32)
        expires = _dt.now(timezone.utc) + timedelta(days=7)
        cur.execute("INSERT INTO auth_tokens (token, user_id, expires_at) VALUES (?,?,?)", (token, user["id"], expires.isoformat()))
    except Exception:
        pass
    conn.commit()
    # Return new token
    cur.execute("SELECT token FROM auth_tokens WHERE user_id=? ORDER BY created_at DESC LIMIT 1", (user["id"],))
    tok_row = cur.fetchone()
    conn.close()
    return {"ok": True, "token": tok_row["token"] if tok_row else None, "message": "Şifre güncellendi — diğer oturumlar kapatıldı"}

# ---------- Company Branding (per-account, used by AI) ----------
def get_company_settings_for_user(user_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM company_settings WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return dict(row)

def _company_ai_context(company):
    if not company or not company.get("company_name"):
        return ""
    parts = [f"Şirket: {company['company_name']}"]
    if company.get("slogan"):
        parts.append(f"Slogan: {company['slogan']}")
    if company.get("website"):
        parts.append(f"Website: {company['website']}")
    return " | ".join(parts)

@app.get("/api/company-settings")
def get_company_settings(user = Depends(get_current_user)):
    cfg = get_company_settings_for_user(user["id"])
    if not cfg:
        return {"exists": False, "data": None}
    return {"exists": True, "data": cfg}

@app.post("/api/company-settings")
def save_company_settings(payload: dict, user = Depends(get_current_user)):
    # Only update provided fields, preserve logo if not sent
    has_logo_key = "logo_url" in payload
    has_logo_file_key = "logo_filename" in payload
    company_name = str(payload.get("company_name","")).strip()
    slogan = str(payload.get("slogan","")).strip()
    website = str(payload.get("website","")).strip()
    email = str(payload.get("email","")).strip()
    phone = str(payload.get("phone","")).strip()
    address = str(payload.get("address","")).strip()
    primary_color = str(payload.get("primary_color","") or "#4f46e5").strip()
    use_in_ai = int(payload.get("use_in_ai", 1)) if "use_in_ai" in payload else 1
    logo_url = str(payload.get("logo_url","")).strip() if has_logo_key else None
    logo_filename = str(payload.get("logo_filename","")).strip() if has_logo_file_key else None
    # Validate color hex
    if not _auth_re.match(r"^#[0-9a-fA-F]{6}$", primary_color):
        primary_color = "#4f46e5"
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM company_settings WHERE user_id=?", (user["id"],))
    existing = cur.fetchone()
    if existing:
        # Preserve logo if not provided in payload
        if logo_url is None:
            logo_url = existing["logo_url"] or ""
        if logo_filename is None:
            logo_filename = existing["logo_filename"] or ""
        # If payload explicitly sent empty string, respect it (clear), but if key missing, keep existing
        # For other fields, allow empty to clear
        cur.execute("""
            UPDATE company_settings SET company_name=?, slogan=?, website=?, email=?, phone=?, address=?, primary_color=?, logo_url=?, logo_filename=?, use_in_ai=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?
        """, (company_name, slogan, website, email, phone, address, primary_color, logo_url, logo_filename, use_in_ai, user["id"]))
    else:
        # Use defaults for missing logo
        if logo_url is None:
            logo_url = ""
        if logo_filename is None:
            logo_filename = ""
        cur.execute("""
            INSERT INTO company_settings (user_id, company_name, slogan, website, email, phone, address, primary_color, logo_url, logo_filename, use_in_ai)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (user["id"], company_name, slogan, website, email, phone, address, primary_color, logo_url, logo_filename, use_in_ai))
    conn.commit()
    # Return updated
    cur.execute("SELECT * FROM company_settings WHERE user_id=?", (user["id"],))
    row = dict(cur.fetchone())
    conn.close()
    return {"ok": True, "data": row, "message": "Şirket ayarları kaydedildi"}

@app.post("/api/company-logo")
async def upload_company_logo(file: UploadFile = File(...), user = Depends(get_current_user)):
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in {".png",".jpg",".jpeg",".gif",".webp",".svg"}:
        raise HTTPException(status_code=400, detail=f"Desteklenmeyen logo türü {ext}. İzin verilenler: png, jpg, jpeg, gif, webp, svg")
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Logo çok büyük (maks. 5MB)")
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Boş dosya")
    filename = f"logo_{user['id']}_{uuid.uuid4().hex}{ext}"
    dest = os.path.join(UPLOAD_DIR, filename)
    with open(dest, "wb") as f:
        f.write(content)
    url = f"/static/uploads/{filename}"
    # Update company_settings with new logo
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM company_settings WHERE user_id=?", (user["id"],))
    existing = cur.fetchone()
    if existing:
        cur.execute("UPDATE company_settings SET logo_url=?, logo_filename=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (url, filename, user["id"]))
    else:
        cur.execute("INSERT INTO company_settings (user_id, logo_url, logo_filename, primary_color) VALUES (?,?,?,?)", (user["id"], url, filename, "#4f46e5"))
    conn.commit()
    conn.close()
    return {"ok": True, "url": url, "filename": filename, "message": "Logo yüklendi"}

@app.delete("/api/company-logo")
def delete_company_logo(user = Depends(get_current_user)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT logo_filename FROM company_settings WHERE user_id=?", (user["id"],))
    row = cur.fetchone()
    if row and row["logo_filename"]:
        fpath = os.path.join(UPLOAD_DIR, row["logo_filename"])
        try:
            if os.path.exists(fpath):
                os.remove(fpath)
        except Exception:
            pass
        cur.execute("UPDATE company_settings SET logo_url='', logo_filename='', updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (user["id"],))
        conn.commit()
    conn.close()
    return {"ok": True, "message": "Logo silindi"}

# ---------- Excel Upload ----------
@app.post("/api/upload-excel")
async def upload_excel(file: UploadFile = File(...), user = Depends(get_current_user)):
    if not file.filename.lower().endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(status_code=400, detail="Sadece .xlsx, .xls, .csv dosyalarına izin verilir")
    content = await file.read()
    try:
        result = parse_excel_file(content, file.filename)
        # Limit preview to 10 rows for response, but keep all records for campaign use if needed
        preview = result["records"][:10]
        return {
            "columns": result["columns"],
            "preview": preview,
            "total_rows": result["total_rows"],
            "detected_email_col": result["detected_email_col"],
            # return full records as well for frontend to use when creating campaign
            "records": result["records"]
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Dosya ayrıştırılamadı: {str(e)}")

# ---------- SMTP Settings ----------
@app.get("/api/smtp-settings")
def get_smtp_settings(user = Depends(get_current_user)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM smtp_settings WHERE user_id=? ORDER BY id DESC LIMIT 1", (user["id"],))
    row = cur.fetchone()
    conn.close()
    if not row:
        return {"exists": False}
    data = dict(row)
    # Don't expose password fully? but we do for editing - mask partially
    return {"exists": True, "data": data}

@app.post("/api/smtp-settings")
def save_smtp_settings(payload: dict, user = Depends(get_current_user)):
    required = ["host", "port", "sender_email"]
    for f in required:
        if not payload.get(f):
            raise HTTPException(status_code=400, detail=f"Eksik alan: {f}")
    # Validate host looks like a hostname
    host = str(payload.get("host","")).strip()
    if " " in host or "." not in host:
        raise HTTPException(status_code=400, detail=f"SMTP Sunucusu '{host}' geçersiz. smtp.gmail.com (Gmail için) veya smtp.office365.com (Outlook için) gibi bir sunucu adresi olmalı, şirket adı değil. Lütfen SMTP Sunucusu alanını kontrol edin.")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM smtp_settings WHERE user_id=? LIMIT 1", (user["id"],))
    existing = cur.fetchone()
    if existing:
        cur.execute("""
            UPDATE smtp_settings SET host=?, port=?, username=?, password=?, sender_email=?, sender_name=?, use_tls=?, use_ssl=?, updated_at=CURRENT_TIMESTAMP WHERE id=?
        """, (
            payload["host"], int(payload["port"]), payload.get("username",""), payload.get("password",""), payload["sender_email"], payload.get("sender_name",""), int(payload.get("use_tls",1)), int(payload.get("use_ssl",0)), existing["id"]
        ))
    else:
        cur.execute("""
            INSERT INTO smtp_settings (host, port, username, password, sender_email, sender_name, use_tls, use_ssl, user_id) VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            payload["host"], int(payload["port"]), payload.get("username",""), payload.get("password",""), payload["sender_email"], payload.get("sender_name",""), int(payload.get("use_tls",1)), int(payload.get("use_ssl",0)), user["id"]
        ))
    conn.commit()
    conn.close()
    return {"ok": True, "message": "SMTP ayarları kaydedildi."}

@app.post("/api/smtp-test")
def test_smtp(payload: dict, user = Depends(get_current_user)):
    # payload may contain smtp fields + test_email
    test_email = payload.get("test_email")
    if not test_email or "@" not in test_email:
        raise HTTPException(status_code=400, detail="Geçerli bir test e-postası gerekli")
    # Use provided config or saved config
    config = payload
    if not config.get("host"):
        config = get_smtp_config(user["id"])
        if not config:
            raise HTTPException(status_code=400, detail="SMTP yapılandırması bulunamadı")
    # Early host validation for friendlier error
    host = str(config.get("host","")).strip()
    if " " in host or "." not in host:
        raise HTTPException(status_code=400, detail=f"SMTP Sunucusu '{host}' geçersiz. Gmail için smtp.gmail.com kullanın. Şirket adı girdiniz, sunucu adresi değil.")
    try:
        send_single_email(config, test_email, "Mailium Sistemi Test E-postası", "<h3>SMTP Testi Başarılı</h3><p>E-posta ayarlarınız doğru.</p><p>Bu, Mailium Sisteminizden gelen bir test e-postasıdır.</p>", is_html=True)
        return {"ok": True, "message": f"Test e-postası şuraya gönderildi: {test_email}"}
    except Exception as e:
        # Use the improved message from email_service
        raise HTTPException(status_code=500, detail=str(e))

# ---------- Campaigns ----------
@app.post("/api/campaigns")
def create_campaign(payload: dict, user = Depends(get_current_user)):
    required = ["title", "subject", "body", "recipients_data", "columns_data", "schedule_type"]
    for f in required:
        if payload.get(f) is None or payload.get(f) == "":
            # allow empty body? but check
            if f in ["body"] and payload.get(f) == "":
                raise HTTPException(status_code=400, detail=f"Eksik alan: {f}")
            if f not in ["body"] and not payload.get(f):
                raise HTTPException(status_code=400, detail=f"Eksik alan: {f}")

    # Validate recipients
    recipients = payload["recipients_data"]
    if isinstance(recipients, str):
        try:
            recipients = json.loads(recipients)
        except:
            raise HTTPException(status_code=400, detail="Geçersiz alıcı verisi")
    if not isinstance(recipients, list) or len(recipients)==0:
        raise HTTPException(status_code=400, detail="Alıcı listesi boş")

    columns = payload["columns_data"]
    if isinstance(columns, str):
        try:
            columns = json.loads(columns)
        except:
            columns = []

    # Handle attachments (dosya ekleri) — optional list of {filename, url, original, size, ext}
    attachments = payload.get("attachments_data") or payload.get("attachments") or []
    if isinstance(attachments, str):
        try:
            attachments = json.loads(attachments)
        except:
            attachments = []
    if not isinstance(attachments, list):
        attachments = []

    conn = get_db()
    cur = conn.cursor()
    # Ensure attachments_data column exists (migration for old DBs)
    try:
        cur.execute("SELECT attachments_data FROM campaigns LIMIT 1")
    except Exception:
        try:
            cur.execute("ALTER TABLE campaigns ADD COLUMN attachments_data TEXT DEFAULT '[]'")
            conn.commit()
        except Exception:
            pass
    cur.execute("""
        INSERT INTO campaigns (title, subject, body, is_html, email_column, recipients_data, columns_data, schedule_type, schedule_time, day_of_month, month_of_year, day_of_week, status, attachments_data, user_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        payload["title"],
        payload["subject"],
        payload["body"],
        1 if payload.get("is_html", True) else 0,
        payload.get("email_column", "Email"),
        json.dumps(recipients),
        json.dumps(columns),
        payload["schedule_type"],
        payload.get("schedule_time"),
        payload.get("day_of_month"),
        payload.get("month_of_year"),
        payload.get("day_of_week"),
        "scheduled",
        json.dumps(attachments),
        user["id"]
    ))
    cid = cur.lastrowid
    conn.commit()
    # Fetch back
    cur.execute("SELECT * FROM campaigns WHERE id=?", (cid,))
    camp = dict(cur.fetchone())
    conn.close()

    # Schedule it
    schedule_campaign(camp)

    # If immediate, it will run shortly
    return {"ok": True, "campaign": camp, "message": "Kampanya oluşturuldu ve zamanlandı."}

@app.get("/api/campaigns")
def list_campaigns(user = Depends(get_current_user)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM campaigns WHERE user_id=? ORDER BY created_at DESC", (user["id"],))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    # Parse json fields for display
    for r in rows:
        try:
            rec = json.loads(r["recipients_data"])
            r["recipient_count"] = len(rec)
        except:
            r["recipient_count"] = 0
    return rows

@app.get("/api/campaigns/{cid}")
def get_campaign(cid: int, user = Depends(get_current_user)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM campaigns WHERE id=? AND user_id=?", (cid, user["id"]))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Kampanya bulunamadı")
    return dict(row)

@app.delete("/api/campaigns/{cid}")
def delete_campaign(cid: int, user = Depends(get_current_user)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM campaigns WHERE id=? AND user_id=?", (cid, user["id"]))
    if not cur.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Kampanya bulunamadı")
    try:
        scheduler.remove_job(str(cid))
    except:
        pass
    cur.execute("DELETE FROM campaigns WHERE id=?", (cid,))
    cur.execute("DELETE FROM audit_logs WHERE campaign_id=?", (cid,))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/campaigns/{cid}/run-now")
def run_campaign_now(cid: int, user = Depends(get_current_user)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM campaigns WHERE id=? AND user_id=?", (cid, user["id"]))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Kampanya bulunamadı")
    # Run in background? For now run synchronously but quickly
    execute_campaign(cid)
    return {"ok": True, "message": "Kampanya çalıştırıldı."}

@app.post("/api/campaigns/{cid}/preview")
def preview_campaign(cid: int, user = Depends(get_current_user)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM campaigns WHERE id=? AND user_id=?", (cid, user["id"]))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Kampanya bulunamadı")
    camp = dict(row)
    try:
        recipients = json.loads(camp["recipients_data"])
    except:
        recipients = []
    if not recipients:
        return {"previews": []}
    previews = []
    for rec in recipients[:3]:
        previews.append({
            "recipient": rec,
            "subject": render_template(camp["subject"], rec),
            "body": render_template(camp["body"], rec)
        })
    return {"previews": previews}

# ---------- Logs ----------
@app.get("/api/logs")
def get_logs(limit: int = 100, user = Depends(get_current_user)):
    conn = get_db()
    cur = conn.cursor()
    # Only logs for this user's campaigns (user_id column added via migration)
    cur.execute("SELECT * FROM audit_logs WHERE user_id=? OR campaign_id IN (SELECT id FROM campaigns WHERE user_id=?) ORDER BY timestamp DESC LIMIT ?", (user["id"], user["id"], limit))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

@app.delete("/api/logs")
def clear_logs(user = Depends(get_current_user)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM audit_logs WHERE user_id=? OR campaign_id IN (SELECT id FROM campaigns WHERE user_id=?)", (user["id"], user["id"]))
    conn.commit()
    conn.close()
    return {"ok": True}

# ---------- Stats ----------
@app.get("/api/stats")
def get_stats(user = Depends(get_current_user)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM campaigns WHERE user_id=?", (user["id"],))
    total_campaigns = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM campaigns WHERE status='scheduled' AND user_id=?", (user["id"],))
    scheduled = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM audit_logs WHERE status='SUCCESS' AND (user_id=? OR campaign_id IN (SELECT id FROM campaigns WHERE user_id=?))", (user["id"], user["id"]))
    success = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM audit_logs WHERE status='FAILED' AND (user_id=? OR campaign_id IN (SELECT id FROM campaigns WHERE user_id=?))", (user["id"], user["id"]))
    failed = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM audit_logs WHERE user_id=? OR campaign_id IN (SELECT id FROM campaigns WHERE user_id=?)", (user["id"], user["id"]))
    total_sends = cur.fetchone()["c"]
    conn.close()
    return {
        "total_campaigns": total_campaigns,
        "scheduled": scheduled,
        "success": success,
        "failed": failed,
        "total_sends": total_sends
    }

# ---------- Image Upload ----------
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

@app.post("/api/upload-image")
async def upload_image(file: UploadFile = File(...), user = Depends(get_current_user)):
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail=f"Desteklenmeyen görsel türü {ext}. İzin verilenler: {', '.join(ALLOWED_IMAGE_TYPES)}")
    # Validate size (max 5MB)
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Görsel çok büyük (maks. 5MB)")
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Boş dosya")
    filename = f"{uuid.uuid4().hex}{ext}"
    dest = os.path.join(UPLOAD_DIR, filename)
    with open(dest, "wb") as f:
        f.write(content)
    # Return URL accessible via /static/uploads/
    url = f"/static/uploads/{filename}"
    return {"ok": True, "url": url, "filename": filename, "original": file.filename, "size": len(content)}

# Generic file attachment upload (pdf, docx, xlsx, zip, etc.)
ALLOWED_ATTACHMENT_TYPES = {".pdf",".doc",".docx",".xls",".xlsx",".csv",".txt",".zip",".rar",".ppt",".pptx",".png",".jpg",".jpeg",".gif",".webp",".bmp",".svg"}
BLOCKED_EXTS = {".exe",".bat",".sh",".js",".vbs",".ps1"}

@app.post("/api/upload-attachment")
async def upload_attachment(file: UploadFile = File(...), user = Depends(get_current_user)):
    ext = os.path.splitext(file.filename)[1].lower()
    if ext in BLOCKED_EXTS:
        raise HTTPException(status_code=400, detail=f"Bu dosya türüne izin verilmiyor: {ext}")
    if ext and ext not in ALLOWED_ATTACHMENT_TYPES:
        # Allow any other extension but warn? For now allow all except blocked, up to 15MB
        pass
    content = await file.read()
    if len(content) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Dosya çok büyük (maks. 15MB)")
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Boş dosya")
    filename = f"{uuid.uuid4().hex}{ext}"
    dest = os.path.join(UPLOAD_DIR, filename)
    with open(dest, "wb") as f:
        f.write(content)
    url = f"/static/uploads/{filename}"
    return {"ok": True, "url": url, "filename": filename, "original": file.filename, "size": len(content), "ext": ext}

@app.get("/api/uploads")
def list_uploads(user = Depends(get_current_user)):
    files = []
    for fname in os.listdir(UPLOAD_DIR):
        fpath = os.path.join(UPLOAD_DIR, fname)
        if os.path.isfile(fpath):
            files.append({"filename": fname, "url": f"/static/uploads/{fname}", "size": os.path.getsize(fpath)})
    return files

@app.delete("/api/uploads/{filename}")
def delete_upload(filename: str, user = Depends(get_current_user)):
    # Prevent path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Geçersiz dosya adı")
    fpath = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail="Dosya bulunamadı")
    os.remove(fpath)
    return {"ok": True}

# ---------- AI Enhancement (Mailium Asistan) — Gerçek AI: Ollama local (qwen2) + Heuristic Fallback ----------
import re as _re
try:
    import ai_service
    _AI_REAL_AVAILABLE = True
except Exception as e:
    print(f"ai_service not available: {e}")
    ai_service = None
    _AI_REAL_AVAILABLE = False

def _ai_enhance_subject(text: str) -> str:
    if not text.strip():
        return text
    # Keep placeholders {{...}} intact while enhancing
    t = text.strip()
    t = _re.sub(r'\s+', ' ', t)
    # Capitalize first letter if not placeholder
    if t and not t.startswith('{{'):
        t = t[0].upper() + t[1:]
    # Ensure subject is engaging — add prefix if too plain and short
    if len(t) < 15 and '{{' not in t:
        t = f"Özel Davet: {t}"
    # Fix punctuation
    if t.endswith('..'):
        t = t.rstrip('.') + '.'
    # Add personalization hint if no placeholder
    return t

def _ai_enhance_body(html_or_text: str, company=None) -> str:
    # Extract text from HTML for enhancement, then return improved HTML snippet
    # Keep {{placeholders}} intact, use company branding if configured
    original = html_or_text.strip()
    if not original:
        return original
    # Company signature
    company_sig = f"<br><strong>{company['company_name']}</strong>" if company and company.get("company_name") and company.get("use_in_ai") else ""
    # If already HTML, strip tags for analysis but keep placeholders
    text_only = _re.sub(r'<[^>]+>', ' ', original)
    text_only = _re.sub(r'\s+', ' ', text_only).strip()
    # Heuristic improvements for Turkish business email
    # Add greeting if missing
    has_greeting = any(x in text_only.lower() for x in ['merhaba','sayın','selam','günaydın'])
    # Build enhanced version — keep original HTML structure if present, else create paragraphs
    # If original was plain-ish and short, expand to professional template
    if len(text_only.split()) < 20:
        # Expand short text to fuller professional email
        core = text_only
        enhanced = f"<p>Merhaba {{{{Name}}}},</p><p>{core}</p><p>Bu süreçte <strong>{{{{Company}}}}</strong> ile olan iş birliğimizi daha da güçlendirmeyi hedefliyoruz. Detaylar için ekteki dosyayı inceleyebilir veya doğrudan yanıt verebilirsiniz.</p><p>Saygılarımızla,{company_sig}</p>"
        # If original already had placeholders, preserve them
        if '{{Name}}' not in enhanced and '{{Name}}' in original:
            enhanced = enhanced.replace('{{Name}}', '{{Name}}')
        return enhanced
    else:
        # For longer text, polish: ensure paragraphs, greeting, closing
        # If contains HTML tags, try to keep them but ensure greeting/closing
        if '<p>' not in original and '<div>' not in original:
            # Plain text -> wrap paragraphs
            paras = [p.strip() for p in _re.split(r'\n\s*\n|(?<=[.!?])\s{2,}', text_only) if p.strip()]
            if not has_greeting:
                paras.insert(0, "Merhaba {{Name}},")
            if not any('saygılar' in p.lower() or 'teşekkür' in p.lower() for p in paras[-1:]):
                paras.append(f"Saygılarımızla,{company_sig}")
            html = "".join(f"<p>{p}</p>" for p in paras)
            # Restore placeholders case?
            return html
        else:
            # Already HTML — ensure it has greeting and closing
            html = original
            if not has_greeting and 'Merhaba' not in html:
                html = f"<p>Merhaba {{{{Name}}}},</p>" + html
            if 'Saygılar' not in html:
                html += f"<p>Saygılarımızla,{company_sig}</p>"
            # Polish punctuation spacing around placeholders
            html = _re.sub(r'\{\{\s+', '{{', html)
            html = _re.sub(r'\s+\}\}', '}}', html)
            return html

def _ai_generate_fancy_html(content: str, subject: str = "", template: str = "modern", company=None) -> str:
    # Convert plain/content into fancy responsive email HTML — improved with multiple templates
    # Keep placeholders, strip outer HTML if pasted, use company branding if configured
    if '<html' in content.lower():
        m = _re.search(r'<body[^>]*>(.*?)</body>', content, _re.I | _re.S)
        if m:
            content = m.group(1)
    if '<p' in content.lower() or '<div' in content.lower():
        inner = content
    else:
        paras = [p.strip() for p in _re.split(r'\n\s*\n', content) if p.strip()]
        if not paras:
            paras = [content.strip()]
        inner = "".join(f"<p style=\"margin:0 0 16px 0; line-height:1.7; color:#334155; font-size:15px;\">{_re.sub(r'\\n', '<br>', _re.sub(r'&', '&amp;', p).replace('<','&lt;').replace('>','&gt;') if '<' not in p else p)}</p>" for p in paras)
        # Actually preserve raw p text with escape only if needed; simpler: use raw
        inner = "".join(f"<p style=\"margin:0 0 16px 0; line-height:1.7; color:#334155; font-size:15px;\">{_re.sub(r'\\n', '<br>', p)}</p>" for p in paras)
        if 'merhaba' not in inner.lower() and 'sayın' not in inner.lower():
            inner = f"<p style=\"margin:0 0 16px 0; line-height:1.7; color:#334155; font-size:15px;\">Merhaba {{{{Name}}}},</p>" + inner
    subj_esc = (subject or "Özel Mesaj").replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
    # Restore subject html escape for allowed chars
    subj_esc = subject or "Özel Mesaj"
    template = (template or "modern").lower()
    # Company branding for fancy HTML
    use_brand = bool(company and company.get("use_in_ai") and (company.get("company_name") or company.get("logo_url")))
    comp_name = (company.get("company_name") if company else "") or ""
    comp_logo = (company.get("logo_url") if company else "") or ""
    comp_color = (company.get("primary_color") if company and company.get("primary_color") else "#4f46e5")
    # Ensure color is valid hex
    if not _re.match(r"^#[0-9a-fA-F]{6}$", comp_color):
        comp_color = "#4f46e5"
    logo_html = f'<img src="{comp_logo}" alt="{comp_name}" style="max-height:42px; max-width:160px; height:auto; margin:0 auto 10px auto; display:block;">' if comp_logo else ""
    brand_header_name = comp_name if use_brand and comp_name else ""
    # Shared footer - with company if branded
    if use_brand and comp_name:
        footer = f"""<tr><td style="background-color:#f8fafc; padding:20px 32px; text-align:center; border-top:1px solid #e2e8f0;">
        <div style="font-size:12px; color:#64748b; line-height:1.6; font-family:Arial, Helvetica, sans-serif;">
          Bu e-posta {{{{Company}}}} için {comp_name} tarafından gönderildi.<br>
          <span style="color:#94a3b8;">Sorularınız için yanıtlayın • Bu otomatik bir iletidir</span>
        </div>
        <div style="margin-top:10px; font-size:11px; color:#94a3b8; font-family:Arial, Helvetica, sans-serif;">© 2026 {comp_name} • Tüm hakları saklıdır</div>
      </td></tr>"""
    else:
        footer = """<tr><td style="background-color:#f8fafc; padding:20px 32px; text-align:center; border-top:1px solid #e2e8f0;">
        <div style="font-size:12px; color:#64748b; line-height:1.6; font-family:Arial, Helvetica, sans-serif;">
          Bu e-posta {{Company}} için gönderildi.<br>
          <span style="color:#94a3b8;">Sorularınız için yanıtlayın • Bu otomatik bir iletidir</span>
        </div>
        <div style="margin-top:10px; font-size:11px; color:#94a3b8; font-family:Arial, Helvetica, sans-serif;">© 2026 Tüm hakları saklıdır</div>
      </td></tr>"""
    cta = """<table cellpadding="0" cellspacing="0" border="0" style="margin-top:24px; width:100%;"><tr><td align="center">
          <a href="#" style="display:inline-block; background-color:{cta_bg}; color:{cta_color}; padding:12px 28px; border-radius:999px; text-decoration:none; font-weight:bold; font-size:14px; font-family:Arial, Helvetica, sans-serif; border:{cta_border};">Hemen İncele →</a>
        </td></tr></table>"""
    body_wrap = """<div style="font-size:15px; color:#334155; line-height:1.75; font-family:Arial, Helvetica, sans-serif;">{inner}</div>"""
    # Branding helpers for templates
    header_logo_block = f'<div style="text-align:center; margin-bottom:10px;">{logo_html}</div>' if logo_html else ""
    header_brand_text = f'<div style="font-size:11px; letter-spacing:2px; color:#ffffff; opacity:0.9; margin-top:6px;">{comp_name}</div>' if use_brand and comp_name else ""
    # Bottom insignia — company logo + name at footer, always visible when branded
    if use_brand and (comp_logo or comp_name):
        bottom_insignia = f"""<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:28px; padding-top:20px; border-top:1px solid #e2e8f0;"><tr><td align="center" style="text-align:center;">
            {f'<img src="{comp_logo}" alt="{comp_name}" style="max-height:36px; max-width:140px; height:auto; margin:0 auto 8px auto; display:block;">' if comp_logo else ""}
            <div style="font-size:13px; font-weight:700; color:#334155; font-family:Arial, Helvetica, sans-serif;">{comp_name if comp_name else ""}</div>
            {f'<div style="font-size:11px; color:#64748b; margin-top:2px;">{company.get("slogan","")}</div>' if company and company.get("slogan") else ""}
            {f'<div style="font-size:11px; color:#64748b; margin-top:4px;"><a href="{company.get("website","")}" style="color:{comp_color}; text-decoration:none;">{company.get("website","")}</a></div>' if company and company.get("website") else ""}
        </td></tr></table>"""
    else:
        bottom_insignia = ""
    # CTA colors per template, override with company color when branded
    # For minimal/bold etc., we keep their style but use company color if branded and template is modern/minimal
    def _cta_for(template_name):
        if use_brand:
            return (comp_color, "#ffffff", f"1px solid {comp_color}")
        # defaults
        if template_name == "minimal":
            return ("#0f172a", "#ffffff", "1px solid #0f172a")
        if template_name == "bold":
            return ("#f59e0b", "#0f172a", "1px solid #f59e0b")
        if template_name == "elegant":
            return ("#422006", "#fefce8", "1px solid #422006")
        if template_name == "newsletter":
            return ("#4f46e5", "#ffffff", "1px solid #4f46e5")
        return ("#4f46e5", "#ffffff", "1px solid #4f46e5")

    if template == "minimal":
        cta_bg, cta_color, cta_border = _cta_for("minimal")
        fancy = f"""<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#ffffff; padding:32px 0; font-family:Arial, Helvetica, sans-serif;">
  <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0" border="0" style="background-color:#ffffff; width:600px; max-width:92%; margin:0 auto;">
      <tr><td style="padding:24px 32px 12px 32px; border-bottom:1px solid #f1f5f9;">
        {header_logo_block}
        <div style="font-size:13px; letter-spacing:3px; color:#94a3b8; font-weight:600;">{comp_name if use_brand and comp_name else "E-POSTA"}</div>
        <div style="font-size:22px; font-weight:700; color:#0f172a; margin-top:6px;">{subj_esc}</div>
        <div style="width:32px; height:2px; background-color:{comp_color if use_brand else "#0f172a"}; margin-top:12px;"></div>
      </td></tr>
      <tr><td style="padding:28px 32px;">
        {body_wrap.format(inner=inner)}
        {cta.format(cta_bg=cta_bg, cta_color=cta_color, cta_border=cta_border)}
        {bottom_insignia}
      </td></tr>
      {footer}
    </table>
  </td></tr>
</table>
<div style="display:none; max-height:0; overflow:hidden; opacity:0;">{subj_esc}</div>"""
        return fancy
    elif template == "bold":
        cta_bg, cta_color, cta_border = _cta_for("bold")
        fancy = f"""<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#0f172a; padding:24px 0; font-family:Arial, Helvetica, sans-serif;">
  <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0" border="0" style="background-color:#ffffff; border-radius:12px; overflow:hidden; width:600px; max-width:92%; margin:0 auto; border:1px solid #1e293b;">
      <tr><td style="background-color:#0f172a; padding:32px; text-align:center;">
        {header_logo_block}
        <div style="display:inline-block; background-color:{comp_color if use_brand else "#f59e0b"}; color:#0f172a; font-size:11px; font-weight:800; letter-spacing:1.5px; padding:4px 10px; border-radius:999px;">{comp_name if use_brand and comp_name else "BİLGİLENDİRME"}</div>
        <div style="font-size:24px; font-weight:800; color:#ffffff; margin-top:14px; line-height:1.2;">{subj_esc}</div>
        <div style="width:40px; height:3px; background-color:{comp_color if use_brand else "#f59e0b"}; margin:14px auto 0 auto;"></div>
      </td></tr>
      <tr><td style="padding:32px; background-color:#ffffff;">
        {body_wrap.format(inner=inner)}
        {cta.format(cta_bg=cta_bg, cta_color=cta_color, cta_border=cta_border)}
        {bottom_insignia}
      </td></tr>
      {footer}
    </table>
  </td></tr>
</table>"""
        return fancy
    elif template == "elegant":
        fancy = f"""<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#fefce8; padding:28px 0; font-family:Arial, Helvetica, sans-serif;">
  <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0" border="0" style="background-color:#ffffff; width:600px; max-width:92%; margin:0 auto; border:1px solid #fde68a;">
      <tr><td style="padding:28px 32px 8px 32px; text-align:center;">
        {header_logo_block}
        <div style="font-size:11px; letter-spacing:4px; color:#a16207; font-weight:600;">— {comp_name if use_brand and comp_name else "BİLGİLENDİRME"} —</div>
        <div style="font-size:11px; letter-spacing:2px; color:#ca8a04; margin-top:2px;">{comp_name if use_brand and comp_name else "E-POSTA"}</div>
      </td></tr>
      <tr><td style="padding:8px 32px 0 32px;"><div style="height:1px; background-color:#fde68a;"></div></td></tr>
      <tr><td style="padding:20px 32px 4px 32px; text-align:center;">
        <div style="font-size:20px; font-weight:700; color:#422006; font-family:Georgia, serif;">{subj_esc}</div>
      </td></tr>
      <tr><td style="padding:24px 32px;">
        {body_wrap.format(inner=inner)}
        {cta.format(cta_bg=_cta_for("elegant")[0], cta_color=_cta_for("elegant")[1], cta_border=_cta_for("elegant")[2])}
        {bottom_insignia}
      </td></tr>
      {footer}
    </table>
  </td></tr>
</table>"""
        return fancy
    elif template == "newsletter":
        cta_bg, cta_color, cta_border = _cta_for("newsletter")
        fancy = f"""<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f8fafc; padding:24px 0; font-family:Arial, Helvetica, sans-serif;">
  <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0" border="0" style="background-color:#ffffff; width:600px; max-width:92%; margin:0 auto; border:1px solid #e2e8f0;">
      <tr><td style="padding:18px 32px; border-bottom:1px solid #e2e8f0;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
          <td style="font-size:11px; letter-spacing:2px; color:#64748b; font-weight:700;">{comp_name if use_brand and comp_name else "BÜLTEN"}</td>
          <td align="right" style="font-size:11px; color:#94a3b8;">Sayı #{{{{Month}}}} • 2026</td>
        </tr></table>
      </td></tr>
      <tr><td style="padding:28px 32px 8px 32px;">
        {header_logo_block}
        <div style="display:inline-block; background-color:#eef2ff; color:#4f46e5; font-size:11px; font-weight:700; padding:4px 10px; border-radius:6px; letter-spacing:0.5px;">ÖNE ÇIKAN</div>
        <div style="font-size:24px; font-weight:800; color:#0f172a; margin-top:12px; line-height:1.25;">{subj_esc}</div>
        <div style="font-size:13px; color:#64748b; margin-top:6px;">Merhaba {{{{Name}}}} — {{{{Company}}}} için hazırladık</div>
      </td></tr>
      <tr><td style="padding:16px 32px;">
        {body_wrap.format(inner=inner)}
        {cta.format(cta_bg=cta_bg, cta_color=cta_color, cta_border=cta_border)}
        {bottom_insignia}
      </td></tr>
      {footer}
    </table>
  </td></tr>
</table>"""
        return fancy
    else:  # modern - with company branding when configured
        header_bg = comp_color if use_brand else "#4f46e5"
        cta_bg_mod, cta_color_mod, cta_border_mod = _cta_for("modern")
        fancy = f"""<div style="display:none; max-height:0; overflow:hidden; opacity:0;">{subj_esc} — Merhaba {{{{Name}}}}</div>
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f1f5f9; padding:24px 0; font-family:Arial, Helvetica, sans-serif;">
  <tr><td align="center" style="padding:0;">
    <table width="600" cellpadding="0" cellspacing="0" border="0" style="background-color:#ffffff; border:1px solid #e2e8f0; width:600px; max-width:92%; margin:0 auto; border-radius:12px; overflow:hidden;">
      <tr><td style="background-color:{header_bg}; padding:28px 32px; text-align:center;">
        {header_logo_block}
        <div style="font-size:12px; letter-spacing:2px; color:#c7d2fe; font-weight:600;">{comp_name if use_brand and comp_name else "E-POSTA"}</div>
        <div style="font-size:22px; font-weight:800; color:#ffffff; margin-top:4px;">{comp_name if use_brand and comp_name else subj_esc}</div>
        <div style="margin-top:14px; display:inline-block; background-color:rgba(255,255,255,0.15); color:#ffffff; font-size:13px; font-weight:600; padding:6px 14px; border-radius:999px;">{subj_esc}</div>
      </td></tr>
      <tr><td style="padding:32px; background-color:#ffffff;">
        {body_wrap.format(inner=inner)}
        {cta.format(cta_bg=cta_bg_mod, cta_color=cta_color_mod, cta_border=cta_border_mod)}
        {bottom_insignia}
        <div style="margin-top:24px; padding:14px 16px; background-color:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; font-size:12px; color:#64748b; line-height:1.6;">
          <strong style="color:#334155;">İpucu:</strong> Bu e-posta {{{{Company}}}} için kişiselleştirildi. Yanıtlayarak bize ulaşabilirsiniz.
        </div>
      </td></tr>
      {footer}
    </table>
  </td></tr>
</table>"""
        return fancy

def _heuristic_compose(prompt: str, tone: str = "professional", company=None) -> dict:
    """Fallback compose when real AI unavailable. Uses company branding if configured."""
    p = (prompt or "").strip()
    if not p:
        return {"subject": "", "body": ""}
    company_sig = f"<br><strong>{company['company_name']}</strong>" if company and company.get("company_name") and company.get("use_in_ai") else ""
    # Tone-based subject prefix
    tone_titles = {
        "professional": "Bilgilendirme",
        "friendly": "Merhaba {{Name}} — küçük bir hatırlatma",
        "formal": "Sayın {{Name}} — Resmi Bilgilendirme",
        "casual": "Selam {{Name}}!",
        "persuasive": "Kaçırmayın, {{Name}} — özel fırsat"
    }
    # Infer subject from prompt keywords
    low = p.lower()
    if any(k in low for k in ["fatura", "ödeme", "tahsilat", "borç"]):
        subject = "Ödeme Hatırlatması — {{Company}} için fatura bilgisi"
        body_core = f"<p>{p}</p><p><strong>{{{{Company}}}}</strong> adına kayıtlı <strong>{{{{Amount}}}} TL</strong> tutarındaki faturanızın <strong>{{{{Month}}}}</strong> dönemi için son ödeme tarihini hatırlatmak isteriz.</p>"
    elif any(k in low for k in ["davet", "etkinlik", "toplantı", "webinar"]):
        subject = "Davetlisiniz — {{Company}} için özel etkinlik"
        body_core = f"<p>{p}</p><p>Sizi ve <strong>{{{{Company}}}}</strong> ekibini aramızda görmekten memnuniyet duyarız. Detaylar ve kayıt için aşağıdaki butona tıklayabilirsiniz.</p>"
    elif any(k in low for k in ["kampanya", "indirim", "fırsat", "teklif"]):
        subject = "Özel Teklif — {{Company}} için % indirim"
        body_core = f"<p>{p}</p><p>Bu kampanya <strong>{{{{Company}}}}</strong> için özel olarak hazırlandı. Kontenjan sınırlı — hemen inceleyin.</p>"
    elif any(k in low for k in ["teşekkür", "hoş geldin", "welcome"]):
        subject = "Hoş geldiniz, {{Name}}!"
        body_core = f"<p>{p}</p><p><strong>{{{{Company}}}}</strong> ailesine katıldığınız için teşekkür ederiz.</p>"
    else:
        subject = tone_titles.get(tone, "Bilgilendirme") + f": {p[:42]}"
        if len(subject) > 80:
            subject = subject[:77] + "..."
        body_core = f"<p>{p}</p><p>Bu konuda <strong>{{{{Company}}}}</strong> ile iş birliğimizi güçlendirmek için yanınızdayız. Sorularınız için doğrudan yanıt verebilirsiniz.</p>"
    body = f"<p>Merhaba {{{{Name}}}},</p>{body_core}<p>Detaylar için ekteki dosyayı inceleyebilir veya bu e-postayı yanıtlayabilirsiniz.</p><p>Saygılarımızla,{company_sig}</p>"
    # Friendly tone: shorter
    if tone == "casual":
        body = body.replace("Saygılarımızla,", "Sevgiler,")
    return {"subject": subject, "body": body}

def _heuristic_fix_grammar(text: str, field: str = "body") -> str:
    """Simple grammar / spelling fixes without AI — preserves placeholders and html."""
    if not text or not text.strip():
        return text
    # Preserve placeholders temporarily
    placeholders = []
    def ph_store(m):
        placeholders.append(m.group(0))
        return f"__PH_{len(placeholders)-1}__"
    tmp = _re.sub(r"\{\{\s*[^}]+\s*\}\}", ph_store, text)
    if field == "subject":
        t = tmp.strip()
        t = _re.sub(r"\s+", " ", t)
        t = _re.sub(r"\s+([,.!?;:])", r"\1", t)
        t = _re.sub(r"([,.!?;:])([^\s])", r"\1 \2", t)
        if t and not t[0].isupper() and not t.startswith("__PH"):
            t = t[0].upper() + t[1:]
        # Capitalize after period
        t = _re.sub(r"([.!?]\s+)([a-zğüşöçı])", lambda m: m.group(1) + m.group(2).upper(), t)
        t = _re.sub(r"\bi\b", "İ", t)
        # Restore placeholders
        for i, ph in enumerate(placeholders):
            t = t.replace(f"__PH_{i}__", ph)
        return t
    else:
        # body — may contain html tags, fix only text nodes
        # If contains html tags, split by tags and fix text parts
        if "<" in tmp and ">" in tmp:
            parts = _re.split(r"(<[^>]+>)", tmp)
            out = []
            for part in parts:
                if not part:
                    continue
                if part.startswith("<"):
                    out.append(part)
                else:
                    # Fix text node: collapse spaces, punctuation spacing, capitalizations
                    txt = part
                    # Keep single leading/trailing space for inline context
                    lead = " " if txt[:1] == " " else ""
                    trail = " " if txt[-1:] == " " and len(txt) > 1 else ""
                    txt_stripped = txt.strip()
                    if not txt_stripped:
                        out.append(part)
                        continue
                    txt_stripped = _re.sub(r"\s+", " ", txt_stripped)
                    txt_stripped = _re.sub(r"\s+([,.!?;:])", r"\1", txt_stripped)
                    txt_stripped = _re.sub(r"([,.!?;:])([^\s<])", r"\1 \2", txt_stripped)
                    # Fix common Turkish typos
                    fixes = {
                        r"\bherkez\b": "herkes",
                        r"\byanlız\b": "yalnız",
                        r"\bşey\b": "şey",
                        r"\bki\s+de\b": "ki de",
                        r"\bde\s+da\b": "de de",
                    }
                    for pat, repl in fixes.items():
                        txt_stripped = _re.sub(pat, repl, txt_stripped, flags=_re.I)
                    out.append(lead + txt_stripped + trail)
            fixed = "".join(out)
        else:
            t = _re.sub(r"\s+", " ", tmp.strip())
            t = _re.sub(r"\s+([,.!?;:])", r"\1", t)
            fixed = t
        for i, ph in enumerate(placeholders):
            fixed = fixed.replace(f"__PH_{i}__", ph)
        # Clean placeholder spacing
        fixed = _re.sub(r"\{\{\s+", "{{", fixed)
        fixed = _re.sub(r"\s+\}\}", "}}", fixed)
        return fixed

@app.post("/api/ai/enhance")
async def ai_enhance(payload: dict, user = Depends(get_current_user)):
    text = payload.get("text", "") or payload.get("body", "") or payload.get("subject", "")
    field = payload.get("field", "body")  # subject or body
    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="İyileştirilecek metin boş olamaz")
    # Try real AI first (per-user LLM config)
    try:
        if _AI_REAL_AVAILABLE and ai_service:
            if field == "subject":
                real = ai_service.real_ai_enhance_subject(text, user_id=user["id"])
                if real and real.strip():
                    return {"ok": True, "enhanced": real, "field": field, "provider": "real", "model": ai_service.get_ai_status(user_id=user["id"]).get("model")}
            elif field == "body":
                real = ai_service.real_ai_enhance_body(text, user_id=user["id"])
                if real and real.strip():
                    return {"ok": True, "enhanced": real, "field": field, "provider": "real", "model": ai_service.get_ai_status(user_id=user["id"]).get("model")}
            else:
                subj = payload.get("subject", "")
                body = payload.get("body", "")
                r_subj = ai_service.real_ai_enhance_subject(subj, user_id=user["id"]) if subj else ""
                r_body = ai_service.real_ai_enhance_body(body, user_id=user["id"]) if body else ""
                # fallback if empty
                if not r_subj: r_subj = _ai_enhance_subject(subj) if subj else ""
                if not r_body: r_body = _ai_enhance_body(body) if body else ""
                return {"subject": r_subj, "body": r_body, "ok": True, "provider": "real"}
    except Exception as e:
        print(f"Real AI enhance failed, fallback to heuristic: {e}")
    # Fallback heuristic with company branding
    company = get_company_settings_for_user(user["id"])
    if field == "subject":
        enhanced = _ai_enhance_subject(text)
    elif field == "body":
        enhanced = _ai_enhance_body(text, company=company)
    else:
        subj = payload.get("subject", "")
        body = payload.get("body", "")
        return {"subject": _ai_enhance_subject(subj) if subj else "", "body": _ai_enhance_body(body, company=company) if body else "", "ok": True, "provider": "heuristic"}
    return {"ok": True, "enhanced": enhanced, "field": field, "provider": "heuristic"}

@app.post("/api/ai/generate-html")
async def ai_generate_html(payload: dict, user = Depends(get_current_user)):
    content = payload.get("content", "") or payload.get("body", "") or payload.get("text", "")
    subject = payload.get("subject", "")
    template = (payload.get("template") or payload.get("style") or "modern").strip().lower()
    if template not in ("modern","minimal","bold","elegant","newsletter"):
        template = "modern"
    if not content or not content.strip():
        raise HTTPException(status_code=400, detail="HTML'e dönüştürülecek içerik boş olamaz")
    company = get_company_settings_for_user(user["id"])
    # Try real AI first (per-user, with company branding)
    try:
        if _AI_REAL_AVAILABLE and ai_service:
            real = ai_service.real_ai_generate_fancy_html(content, subject, template=template, user_id=user["id"])
            if real and real.strip():
                # Real AI may return markdown fences, strip them
                import re as _re2
                rs = _re2.sub(r'^```(?:html)?\s*', '', real.strip(), flags=_re2.I)
                rs = _re2.sub(r'\s*```$', '', rs.strip())
                if "<table" in rs.lower() or "<div" in rs.lower():
                    return {"ok": True, "html": rs, "provider": "real", "model": ai_service.get_ai_status(user_id=user["id"]).get("model"), "template": template}
    except Exception as e:
        print(f"Real AI generate-html failed: {e}")
    html = _ai_generate_fancy_html(content, subject, template=template, company=company)
    return {"ok": True, "html": html, "provider": "heuristic", "template": template}

@app.post("/api/ai/compose")
async def ai_compose(payload: dict, user = Depends(get_current_user)):
    prompt = (payload.get("prompt") or payload.get("text") or payload.get("message") or "").strip()
    tone = (payload.get("tone") or "professional").strip().lower()
    if not prompt:
        raise HTTPException(status_code=400, detail="Oluşturmak için bir talimat yazın (ör. 'fatura hatırlatma e-postası yaz')")
    if len(prompt) < 5:
        raise HTTPException(status_code=400, detail="Talimat çok kısa, biraz daha detay verin")
    language = payload.get("language") or "tr"
    extra = payload.get("extra") or payload.get("context") or ""
    company = get_company_settings_for_user(user["id"])
    # Try real AI first (per-user, with company branding)
    try:
        if _AI_REAL_AVAILABLE and ai_service:
            real = ai_service.real_ai_compose(prompt, tone=tone, language=language, extra_context=extra, user_id=user["id"])
            if real and real.get("subject") and real.get("body"):
                return {"ok": True, "subject": real["subject"], "body": real["body"], "provider": "real", "model": ai_service.get_ai_status(user_id=user["id"]).get("model"), "tone": tone}
    except Exception as e:
        print(f"Real AI compose failed: {e}")
    # Heuristic fallback with company branding
    fallback = _heuristic_compose(prompt, tone=tone, company=company)
    return {"ok": True, "subject": fallback["subject"], "body": fallback["body"], "provider": "heuristic", "tone": tone}

@app.post("/api/ai/fix-grammar")
async def ai_fix_grammar(payload: dict, user = Depends(get_current_user)):
    text = payload.get("text", "") or payload.get("body", "") or payload.get("subject", "") or payload.get("content", "")
    field = payload.get("field", "body")
    # Determine field if subject-focused prompt
    if payload.get("field") not in ("subject","body"):
        # auto-detect: if text short and no html, treat as subject if len<120
        if text and len(text.strip()) < 120 and "<p" not in text.lower() and "<div" not in text.lower():
            # caller can explicitly set field, but default body is safer for email content
            field = payload.get("field") or "body"
        else:
            field = "body"
    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="Düzeltilecek metin boş olamaz")
    # Try real AI first (per-user)
    try:
        if _AI_REAL_AVAILABLE and ai_service:
            real = ai_service.real_ai_fix_grammar(text, field=field, user_id=user["id"])
            if real and real.strip():
                return {"ok": True, "fixed": real, "field": field, "provider": "real", "model": ai_service.get_ai_status(user_id=user["id"]).get("model")}
    except Exception as e:
        print(f"Real AI fix-grammar failed: {e}")
    fixed = _heuristic_fix_grammar(text, field=field)
    return {"ok": True, "fixed": fixed, "field": field, "provider": "heuristic"}

@app.get("/api/ai/status")
def ai_status(user = Depends(get_current_user_optional)):
    try:
        if _AI_REAL_AVAILABLE and ai_service:
            uid = user["id"] if user else None
            s = ai_service.get_ai_status(user_id=uid) if uid else ai_service.get_ai_status()
            s["heuristic_fallback"] = True
            return s
    except Exception as e:
        return {"real_ai": False, "error": str(e), "heuristic_fallback": True}
    return {"real_ai": False, "heuristic_fallback": True, "model": None}

@app.get("/api/ai/config")
def ai_get_config(user = Depends(get_current_user)):
    try:
        if _AI_REAL_AVAILABLE and ai_service:
            return ai_service.get_masked_config(user_id=user["id"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"endpoint": "https://api.openai.com/v1", "model": "gpt-4o-mini", "has_key": False}

@app.post("/api/ai/config")
def ai_save_config(payload: dict, user = Depends(get_current_user)):
    endpoint = payload.get("endpoint", "").strip()
    api_key = payload.get("api_key", "").strip()
    model = payload.get("model", "").strip()
    provider = payload.get("provider", "openai").strip()
    if not endpoint:
        raise HTTPException(status_code=400, detail="Endpoint gerekli (örn. https://api.openai.com/v1)")
    if not api_key:
        raise HTTPException(status_code=400, detail="API anahtarı gerekli")
    if not model:
        model = "gpt-4o-mini"
    try:
        if _AI_REAL_AVAILABLE and ai_service:
            cfg = ai_service.save_ai_config(provider=provider, endpoint=endpoint, api_key=api_key, model=model, user_id=user["id"])
            masked = ai_service.get_masked_config(user_id=user["id"])
            # Test connection quickly with a tiny chat
            test_ok = False
            test_error = ""
            try:
                # quick test with 10 token
                test_resp = ai_service._openai_chat("Sen test asistanısın.", "Merhaba, test 1 2 3", temperature=0.1, max_tokens=10, user_id=user["id"])
                if test_resp:
                    test_ok = True
                else:
                    test_error = "Model yanıt vermedi, endpoint/model kontrol edin"
            except Exception as e:
                test_error = str(e)
            return {"ok": True, "saved": masked, "test_ok": test_ok, "test_error": test_error, "message": "Yapay zeka yapılandırması kaydedildi. Gerçek AI aktif." if test_ok else f"Kaydedildi ama test başarısız: {test_error} — anahtar/endpoint/model kontrol edin."}
        else:
            raise HTTPException(status_code=500, detail="AI servisi yüklenemedi")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/ai/test")
def ai_test(payload: dict = None, user = Depends(get_current_user)):
    try:
        if _AI_REAL_AVAILABLE and ai_service:
            s = ai_service.get_ai_status(user_id=user["id"])
            if not s.get("real_ai"):
                return {"ok": False, "real_ai": False, "message": "Gerçek AI yapılandırılmadı. Ayarlar → Yapay Zeka'dan endpoint ve API anahtarı ekleyin. Şimdilik heuristic çalışıyor."}
            # Try a real call
            resp = ai_service._openai_chat("Sen Mailium asistanısın.", "Merhaba de", temperature=0.3, max_tokens=20, user_id=user["id"]) if ai_service._openai_available(user_id=user["id"]) else ai_service._ollama_chat("Sen test asistanısın.", "Merhaba", temperature=0.3)
            if resp:
                return {"ok": True, "real_ai": True, "provider": s.get("provider"), "model": s.get("model"), "reply": resp[:200]}
            return {"ok": False, "real_ai": False, "message": "AI yanıt vermedi, yapılandırmayı kontrol edin"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": False, "message": "Bilinmeyen durum"}

@app.post("/api/ai/chat")
async def ai_chat(payload: dict, user = Depends(get_current_user)):
    msg = (payload.get("message") or payload.get("text") or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Mesaj boş olamaz")
    # Try real AI first (Ollama local)
    try:
        if _AI_REAL_AVAILABLE and ai_service:
            history = payload.get("history", [])
            real = ai_service.real_ai_chat(msg, history)
            if real and real.strip():
                return {"ok": True, "reply": real, "you": msg, "provider": "real", "model": ai_service.get_ai_status().get("model")}
    except Exception as e:
        print(f"Real AI chat failed: {e}")
    low = msg.lower()
    # Fallback heuristic — App password help
    if any(k in low for k in ["uygulama şifresi", "uygulama sifresi", "app password", "apppassword", "şifre", "sifre", "gmail", "outlook"]):
        reply = """**Uygulama Şifresi (App Password) Nasıl Alınır?**

**Gmail için:**
1. Google Hesabınıza gidin → myaccount.google.com
2. **Güvenlik** → **2 Adımlı Doğrulama**'yı açın (kapalıysa açın)
3. Aynı sayfada **Uygulama şifreleri** → **Uygulama seçin: Posta**, **Cihaz: Diğer (Mailium)** → **Oluştur**
4. 16 karakterli kodu kopyalayın (örn. `aofr pdfi zdju islh` — boşluksuz da yazabilirsiniz)
5. Mailium'da **SMTP Ayarları** → Sunucu: `smtp.gmail.com`, Port: `587`, TLS: açık, Kullanıcı: Gmail adresiniz, Şifre: bu 16 haneli kod, Gönderici e-posta: aynı adres → **Ayarları Kaydet** → **Test E-postası Gönder**

**Outlook/365 için:** Sunucu `smtp.office365.com`, Port `587`, TLS açık.

İpucu: Normal Gmail şifrenizle giriş yaparsanız *535 Hatası* alırsınız — mutlaka Uygulama Şifresi kullanın. Takılırsanız buraya “Gmail 535” yazın, adım adım yönlendireyim!
"""
    elif any(k in low for k in ["navigasyon", "nasıl kullanılır", "nasıl", "adım", "kullanım", "yardım", "help", "nereden", "başla"]):
        reply = """**Mailium'da Nasıl Gezilir? (3 Adım)**

1. **SMTP Ayarları** (sağ menü): Önce Gmail/Outlook sunucunuzu girip *Test E-postası* ile doğrulayın — yeşil tik almadan kampanya gönderilmez.
2. **Yeni Kampanya**: 
   - **Adım 1 Alıcılar**: Excel'i sürükle-bırak (E-posta, Ad, Şirket sütunları). Tablo içinde düzenleyebilir, `×` ile silebilir, **Alıcı Ekle** ile tek tek ekleyebilirsiniz.
   - **Adım 2 Mesaj**: Konu ve gövdeye `{{Name}}` gibi değişken ekleyin. Üstteki **✨ Yazıyı İyileştir** metni profesyonelleştirir, **🎨 Şık HTML'e Dönüştür** düz metni Mailium şablonuna çevirir. Görselleri `🖼️` ile, dosyaları **DOSYA EKLERİ** ile ekleyin.
   - **Adım 3 Zamanlama**: *Hemen*, *Bir Kez*, *Günlük/Haftalık/Aylık/Yıllık* (yerel saat, Avrupa/İstanbul).
3. **Kampanyalar / Kayıtlar**: Oluşan kampanyada `Şimdi Çalıştır / Önizle / Sil`, Kayıtlar'da her alıcı için `SUCCESS/FAILED` görünür.

Hızlı başlatmak için: *Yeni Kampanya → Örnek Excel'i deneyin → **Veriyle Önizle**.*
"""
    elif any(k in low for k in ["zamanlama", "schedule", "günlük", "aylık", "yıllık", "hemen", "bir kez"]):
        reply = """**Zamanlama (Yerel Saat - Avrupa/İstanbul)**

- **Hemen**: Oluşturur oluşturulmaz 2 sn içinde başlar.
- **Bir Kez**: Takvimden tarih-saat seçin (yerel saat). Örn. 2026-09-01 09:00 → 01 Eylül 09:00'da bir kez gider.
- **Günlük / Haftalık / Aylık / Yıllık**: Saat (09:00) ve gün/ay seçimiyle tekrarlar. APScheduler her gün/ay aynı saatte tetikler. Saatler **yerel**dir, Greenwich değil.

Düzenlemek için Kampanyalar → Sil → yeniden oluşturun (zamanlayıcı güncellenir).
"""
    elif any(k in low for k in ["excel", "alıcı", "değişken", "tag", "{{"]):
        reply = """**Excel & Değişkenler**

Excel'de ilk satır **başlık** olmalı: `Email, Name, Company` gibi. Sistem otomatik E-posta sütununu tespit eder (içinde @ varsa). 
Mesajda `{{Name}}`, `{{Company}}`, `{{Email}}` yazdığınız yer her alıcı için kendi satırıyla değişir (büyük/küçük harf duyarsız). *DEĞİŞKEN EKLE* çiplerine tıklayarak ekleyin. Tabloyu satır içinde düzenleyip **Alıcı Ekle** ile yeni kişi ekleyebilirsiniz."""
    elif any(k in low for k in ["ek", "dosya", "attachment", "pdf", "görsel", "resim"]):
        reply = """**Dosya & Görsel Ekleri**

- **Görsel gömme** (e-posta içinde görünsün): Adım 2'de `🖼️` butonu → görsel seç → metin içinde `<img>` olarak gömülür (CID ile, alıcı internet olmadan görür).
- **Dosya eki** (PDF/Word/Excel/ZIP): Adım 2 altındaki **DOSYA EKLERİ** → sürükle-bırak. Maks 15MB/dosya, `.exe` engelli. Kampanyadaki tüm alıcılara aynı dosyalar ek olarak gider. Listede `×` ile kaldırın."""
    else:
        reply = f"""Mailium Asistan buradayım! **“{msg[:40]}”** için yardımcı olayım.

Hızlı sorular:
- **Uygulama şifresi nasıl alınır?** → “uygulama şifresi” yazın
- **Kampanya nasıl oluşturulur?** → “navigasyon” yazın
- **Zamanlama, Excel, ekler, görseller** için o kelimeyi yazın

Ya da doğrudan sorun: *“Gmail'e bağlanamıyorum”*, *“Excel'i nasıl düzenlerim?”*, *“HTML şablon nasıl yapılır?”*"""
    return {"ok": True, "reply": reply, "you": msg, "provider": "heuristic"}

# ---------- Serve Frontend ----------
# Static folder is at project root /static
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
def serve_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return JSONResponse({"message": "API çalışıyor. Arayüz henüz oluşturulmadı. /docs adresine gidin."})
