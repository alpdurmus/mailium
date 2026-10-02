import json
import sqlite3
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
import os

from database import get_db
from email_service import render_template, send_single_email

# Use local system timezone (Europe/Istanbul UTC+3) so "09:00" means 09:00 local, not Greenwich
try:
    from tzlocal import get_localzone
    LOCAL_TZ = get_localzone()
except Exception:
    # Fallback to UTC if tzlocal unavailable — but log it
    import zoneinfo
    try:
        LOCAL_TZ = zoneinfo.ZoneInfo("Europe/Istanbul")
    except Exception:
        LOCAL_TZ = timezone.utc

# APScheduler expects a tzinfo or string; zoneinfo works
scheduler = BackgroundScheduler(timezone=LOCAL_TZ)
scheduler_started = False

def ensure_scheduler():
    global scheduler_started
    if not scheduler_started:
        scheduler.start()
        scheduler_started = True

def log_event(campaign_id, recipient_email, subject, status, message, user_id=None):
    # Try to include user_id if available (per-user isolation)
    # If not provided, try to infer from campaign
    if user_id is None and campaign_id:
        try:
            conn2 = get_db()
            cur2 = conn2.cursor()
            cur2.execute("SELECT user_id FROM campaigns WHERE id=?", (campaign_id,))
            r = cur2.fetchone()
            if r and r["user_id"]:
                user_id = r["user_id"]
            conn2.close()
        except Exception:
            pass
    conn = get_db()
    cur = conn.cursor()
    # Check if user_id column exists
    try:
        cur.execute("SELECT user_id FROM audit_logs LIMIT 1")
        has_user_col = True
    except Exception:
        has_user_col = False
    if has_user_col and user_id is not None:
        cur.execute("INSERT INTO audit_logs (campaign_id, recipient_email, subject, status, message, user_id) VALUES (?,?,?,?,?,?)",
                    (campaign_id, recipient_email, subject, status, message, user_id))
    else:
        cur.execute("INSERT INTO audit_logs (campaign_id, recipient_email, subject, status, message) VALUES (?,?,?,?,?)",
                    (campaign_id, recipient_email, subject, status, message))
    conn.commit()
    conn.close()

def get_smtp_config(user_id=None):
    conn = get_db()
    cur = conn.cursor()
    if user_id is not None:
        cur.execute("SELECT * FROM smtp_settings WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,))
    else:
        cur.execute("SELECT * FROM smtp_settings ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return dict(row)

def execute_campaign(campaign_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return
    campaign = dict(row)
    conn.close()

    smtp_config = get_smtp_config(user_id=campaign.get("user_id"))
    if not smtp_config:
        log_event(campaign_id, "", campaign["subject"], "FAILED", "No SMTP configuration found. Please configure SMTP in Settings.", user_id=campaign.get("user_id"))
        return

    try:
        recipients = json.loads(campaign["recipients_data"])
    except:
        recipients = []

    email_col = campaign["email_column"]
    subject_tpl = campaign["subject"]
    body_tpl = campaign["body"]
    is_html = bool(campaign["is_html"])
    # attachments_data: JSON list of {filename, url, filepath, original, size}
    try:
        attachments = json.loads(campaign.get("attachments_data") or "[]")
        if not isinstance(attachments, list):
            attachments = []
    except:
        attachments = []

    success_count = 0
    fail_count = 0

    for rec in recipients:
        # Find email case-insensitive
        to_email = rec.get(email_col)
        if not to_email:
            # try case-insensitive lookup
            lower_keys = {k.lower(): v for k, v in rec.items()}
            to_email = lower_keys.get(email_col.lower(), "")
        to_email = str(to_email).strip()
        if not to_email or "@" not in to_email:
            log_event(campaign_id, str(to_email), subject_tpl, "FAILED", f"Invalid email for recipient {rec}", user_id=campaign.get("user_id"))
            fail_count += 1
            continue

        subject_rendered = render_template(subject_tpl, rec)
        body_rendered = render_template(body_tpl, rec)

        try:
            send_single_email(smtp_config, to_email, subject_rendered, body_rendered, is_html=is_html, attachments=attachments)
            log_event(campaign_id, to_email, subject_rendered, "SUCCESS", f"Email sent to {to_email}" + (f" + {len(attachments)} ek" if attachments else ""), user_id=campaign.get("user_id"))
            success_count += 1
        except Exception as e:
            log_event(campaign_id, to_email, subject_rendered, "FAILED", str(e), user_id=campaign.get("user_id"))
            fail_count += 1

    # Update campaign status — use local time for display
    conn = get_db()
    cur = conn.cursor()
    try:
        now = datetime.now(LOCAL_TZ).isoformat()
    except Exception:
        now = datetime.now(timezone.utc).isoformat()
    # For one-time / immediate campaigns, mark completed after run
    if campaign["schedule_type"] in ("immediate", "once"):
        cur.execute("UPDATE campaigns SET status='completed', last_run_at=? WHERE id=?", (now, campaign_id))
    else:
        cur.execute("UPDATE campaigns SET last_run_at=?, status='scheduled' WHERE id=?", (now, campaign_id))
    conn.commit()
    conn.close()

    log_event(campaign_id, "", campaign["subject"], "INFO", f"Campaign '{campaign['title']}' executed: {success_count} success, {fail_count} failed.", user_id=campaign.get("user_id"))

def schedule_campaign(campaign: dict):
    ensure_scheduler()
    cid = campaign["id"]
    stype = campaign["schedule_type"]
    stime = campaign.get("schedule_time")
    dom = campaign.get("day_of_month")
    moy = campaign.get("month_of_year")

    # Remove existing job if any
    try:
        scheduler.remove_job(str(cid))
    except:
        pass

    if stype == "immediate":
        # Run 2 seconds from now in LOCAL time
        try:
            run_dt = datetime.now(LOCAL_TZ) + timedelta(seconds=2)
        except Exception:
            run_dt = datetime.now(timezone.utc) + timedelta(seconds=2)
        scheduler.add_job(execute_campaign, DateTrigger(run_date=run_dt), args=[cid], id=str(cid), replace_existing=True, misfire_grace_time=3600)
    elif stype == "once":
        # stime from <input type="datetime-local"> is LOCAL time like 2026-09-01T09:00 (no tz)
        try:
            run_dt = datetime.fromisoformat(stime.replace("Z",""))
            # If naive, interpret as LOCAL time (not UTC) — crucial for 09:00 meaning 09:00 Istanbul
            if run_dt.tzinfo is None:
                try:
                    run_dt = run_dt.replace(tzinfo=LOCAL_TZ)
                except Exception:
                    run_dt = run_dt.replace(tzinfo=timezone.utc)
            scheduler.add_job(execute_campaign, DateTrigger(run_date=run_dt), args=[cid], id=str(cid), replace_existing=True, misfire_grace_time=3600)
        except Exception as e:
            print(f"Invalid once schedule time {stime}: {e}")
    elif stype == "daily":
        # stime "09:00"
        try:
            h, m = map(int, stime.split(":")[:2])
            scheduler.add_job(execute_campaign, CronTrigger(hour=h, minute=m), args=[cid], id=str(cid), replace_existing=True)
        except Exception as e:
            print(f"Invalid daily schedule {stime}: {e}")
    elif stype == "monthly":
        try:
            h, m = map(int, stime.split(":")[:2])
            day = int(dom) if dom else 1
            scheduler.add_job(execute_campaign, CronTrigger(day=day, hour=h, minute=m), args=[cid], id=str(cid), replace_existing=True)
        except Exception as e:
            print(f"Invalid monthly schedule: {e}")
    elif stype == "yearly":
        try:
            h, m = map(int, stime.split(":")[:2])
            day = int(dom) if dom else 1
            month = int(moy) if moy else 1
            scheduler.add_job(execute_campaign, CronTrigger(month=month, day=day, hour=h, minute=m), args=[cid], id=str(cid), replace_existing=True)
        except Exception as e:
            print(f"Invalid yearly schedule: {e}")
    elif stype == "weekly":
        try:
            h, m = map(int, stime.split(":")[:2])
            dow = int(campaign.get("day_of_week", 0))
            scheduler.add_job(execute_campaign, CronTrigger(day_of_week=dow, hour=h, minute=m), args=[cid], id=str(cid), replace_existing=True)
        except Exception as e:
            print(f"Invalid weekly schedule: {e}")

def restore_schedules():
    ensure_scheduler()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM campaigns WHERE status='scheduled'")
    rows = cur.fetchall()
    conn.close()
    for r in rows:
        schedule_campaign(dict(r))
    print(f"Restored {len(rows)} scheduled campaigns.")
