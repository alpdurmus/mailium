import smtplib
import os
import uuid
import mimetypes
import re
from html import unescape
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.base import MIMEBase
from email import encoders

def preserve_html_linebreaks(html: str) -> str:
    """Convert newlines inside HTML text into <br>, matching the compose editor.

    The editor shows line breaks via CSS white-space:pre-wrap. Recipients see HTML,
    where those newlines collapse unless they are <br> or block tags. Whitespace-only
    text between tags is left as a space so pretty-printed markup does not gain extra
    blank lines. Already-<br>'d content is unchanged (idempotent).
    """
    if not html:
        return html
    parts = re.split(r"(<[^>]+>)", html)
    out = []
    for part in parts:
        if not part:
            continue
        if part.startswith("<"):
            out.append(part)
            continue
        if "\n" not in part and "\r" not in part:
            out.append(part)
            continue
        if not part.strip():
            out.append(" ")
            continue
        out.append(part.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>"))
    return "".join(out)


def html_to_plain(html: str) -> str:
    """Plain-text sibling for multipart/alternative — keep visual line breaks."""
    if not html:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", html)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?i)</div\s*>", "\n", text)
    text = re.sub(r"(?i)</h[1-6]\s*>", "\n\n", text)
    text = re.sub(r"(?i)</li\s*>", "\n", text)
    text = re.sub(r"(?i)</tr\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = text.replace("\u200b", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def wrap_html_body(html: str) -> str:
    if not html:
        return html
    if 'role="presentation"' in html and "font-family:Arial" in html:
        return html
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">'
        '<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:#111111;">'
        f"{html}"
        "</td></tr></table>"
    )


def prepare_html_for_email(html: str) -> str:
    html = preserve_html_linebreaks(html or "")
    # Outlook drops empty <p><br></p>; a nbsp keeps the blank line.
    html = re.sub(r"(?i)<p([^>]*)>\s*(?:<br\s*/?>\s*)*</p>", r"<p\1>&nbsp;</p>", html)
    return html


def attach_html_alternative(container, html: str) -> None:
    """RFC 2046: last alternative is preferred. HTML must come after plain."""
    container.attach(MIMEText(html_to_plain(html), "plain", "utf-8"))
    container.attach(MIMEText(wrap_html_body(html), "html", "utf-8"))


def render_template(template_str: str, record_data: dict) -> str:
    """
    Replaces {{ColumnName}} or {{ ColumnName }} placeholders in template_str
    with matching key values from record_data (case-insensitive fallback).
    """
    if not template_str:
        return ""
    
    # Case-insensitive map for matching
    lower_map = {str(k).strip().lower(): str(v) for k, v in record_data.items()}

    def replace_match(match):
        var_name = match.group(1).strip()
        # Direct key lookup
        if var_name in record_data:
            return str(record_data[var_name])
        # Case-insensitive lookup
        var_lower = var_name.lower()
        if var_lower in lower_map:
            return lower_map[var_lower]
        return match.group(0) # Keep original if not found

    return re.sub(r"\{\{\s*([^{}]+?)\s*\}\}", replace_match, template_str)


def send_single_email(smtp_config: dict, to_email: str, subject: str, body: str, is_html: bool = True, attachments: list = None):
    """
    Sends a single email using provided SMTP configuration.
    attachments: list of dicts {filename, filepath, original} or list of filepaths/urls
    """
    host = str(smtp_config.get("host", "")).strip()
    port = int(smtp_config.get("port", 587))
    username = smtp_config.get("username")
    password = smtp_config.get("password")
    sender_email = smtp_config.get("sender_email")
    sender_name = smtp_config.get("sender_name", "")
    use_tls = bool(smtp_config.get("use_tls", 1))
    use_ssl = bool(smtp_config.get("use_ssl", 0))

    if not host or not sender_email:
        raise ValueError("SMTP sunucusu ve gönderici e-postası zorunludur.")
    # Validate host looks like a real SMTP host
    if " " in host or "." not in host:
        raise ValueError(f"SMTP host '{host}' geçersiz. Gmail için smtp.gmail.com, Outlook için smtp.office365.com veya sağlayıcınızın SMTP sunucusunu (örn. smtp.sizinalaniniz.com) kullanın. '{host}' gibi bir şirket adı girdiniz, sunucu adresi değil.")
    if host.lower() in ("timco otomasyon", "timco"):
        raise ValueError(f"SMTP host '{host}' geçersiz. Gmail için sunucu=smtp.gmail.com port=587 TLS açık kullanın.")

    # --- Build message with inline image support ---
    # For HTML, embed any /static/uploads/... images as CID attachments so they load in email clients
    # (localhost URLs would not be reachable from recipient's inbox)
    msg = None
    images_to_embed = []  # list of (cid, filepath)
    html_body_processed = prepare_html_for_email(body) if is_html else body

    if is_html:
        # Find all <img src=".../static/uploads/..."> URLs (both /static/... and absolute http://127.0.0.1:8000/static/...)
        # Regex captures src value
        img_pattern = re.compile(r'<img[^>]+src=["\']([^"\']*?/static/uploads/[^"\']+)["\']', re.IGNORECASE)
        found_srcs = img_pattern.findall(body)
        # Deduplicate
        seen = set()
        unique_srcs = []
        for s in found_srcs:
            if s not in seen:
                seen.add(s)
                unique_srcs.append(s)

        # Base dir for static uploads
        static_uploads_dir = os.path.join(os.path.dirname(__file__), "static", "uploads")
        for src in unique_srcs:
            # Extract filename from URL (strip query string, handle absolute URL)
            # src may be /static/uploads/abc.png or http://127.0.0.1:8000/static/uploads/abc.png
            # Get last part after /static/uploads/
            m = re.search(r'/static/uploads/([^?#"\']+)', src)
            if not m:
                continue
            filename = m.group(1)
            # Security: prevent path traversal
            filename = os.path.basename(filename)
            filepath = os.path.join(static_uploads_dir, filename)
            if not os.path.isfile(filepath):
                continue
            # Generate CID
            cid = f"{uuid.uuid4().hex}@mailforge"
            images_to_embed.append((cid, filepath, src))
            # Replace src in HTML with cid:
            html_body_processed = html_body_processed.replace(src, f"cid:{cid}")

        # Build the core content part (HTML + plain fallback + inline images)
        content_msg = None
        if images_to_embed:
            # related container for HTML + inline images
            content_msg = MIMEMultipart("related")
            # alternative part holds the HTML and plain fallback
            alt = MIMEMultipart("alternative")
            attach_html_alternative(alt, html_body_processed)
            content_msg.attach(alt)
            for cid, filepath, _orig_src in images_to_embed:
                ctype, _enc = mimetypes.guess_type(filepath)
                if ctype is None:
                    ctype = "image/png"
                maintype, subtype = ctype.split("/", 1) if "/" in ctype else ("image", "png")
                with open(filepath, "rb") as f:
                    data = f.read()
                if maintype == "image":
                    img = MIMEImage(data, _subtype=subtype)
                else:
                    img = MIMEBase(maintype, subtype)
                    img.set_payload(data)
                    encoders.encode_base64(img)
                img.add_header("Content-ID", f"<{cid}>")
                img.add_header("Content-Disposition", "inline", filename=os.path.basename(filepath))
                img.add_header("X-Attachment-Id", cid)
                content_msg.attach(img)
        else:
            # No inline images — simple alternative with HTML + plain fallback (better compatibility)
            content_msg = MIMEMultipart("alternative")
            attach_html_alternative(content_msg, html_body_processed)
        # Use content_msg as base for now, will be wrapped in mixed if attachments exist
        msg = content_msg
        # Ensure headers are on the outer msg for now (will be moved if wrapped)
        msg["Subject"] = subject
        if sender_name:
            msg["From"] = f"{sender_name} <{sender_email}>"
        else:
            msg["From"] = sender_email
        msg["To"] = to_email
    else:
        # Plain text — no image embedding
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        if sender_name:
            msg["From"] = f"{sender_name} <{sender_email}>"
        else:
            msg["From"] = sender_email
        msg["To"] = to_email
        msg.attach(MIMEText(body, "plain", "utf-8"))
        # For plain, also add alternative structure correctly
        # (single plain part is fine, but we keep alternative for consistency)

    # --- Generic file attachments (dosya ekleri) — attach as regular attachments ---
    if attachments:
        # First, normalize and collect valid attachments to know if wrapping is needed
        valid_attachments = []
        for att in attachments:
            filepath = None
            filename = None
            if isinstance(att, dict):
                filepath = att.get("filepath") or att.get("path")
                filename = att.get("filename") or att.get("original") or (os.path.basename(filepath) if filepath else None)
                url = att.get("url", "")
                if not filepath and url:
                    m2 = re.search(r'/static/uploads/([^?#"\']+)', url)
                    if m2:
                        fn = os.path.basename(m2.group(1))
                        filepath = os.path.join(os.path.dirname(__file__), "static", "uploads", fn)
                        filename = filename or fn
            elif isinstance(att, str):
                if "/static/uploads/" in att:
                    m2 = re.search(r'/static/uploads/([^?#"\']+)', att)
                    if m2:
                        fn = os.path.basename(m2.group(1))
                        filepath = os.path.join(os.path.dirname(__file__), "static", "uploads", fn)
                        filename = fn
                else:
                    filepath = att
                    filename = os.path.basename(filepath)
            if filepath and os.path.isfile(filepath):
                filename = filename or os.path.basename(filepath)
                valid_attachments.append((filepath, filename))
        # If there are valid attachments and current msg is not mixed, wrap it
        if valid_attachments:
            if msg is None:
                msg = MIMEMultipart("mixed")
                msg["Subject"] = subject
                msg["From"] = f"{sender_name} <{sender_email}>" if sender_name else sender_email
                msg["To"] = to_email
                msg.attach(MIMEText(html_body_processed if is_html else body, "html" if is_html else "plain", "utf-8"))
            elif msg.get_content_type() in ("multipart/alternative", "multipart/related"):
                # Wrap existing content in a mixed container so attachments are siblings, not alternatives
                mixed = MIMEMultipart("mixed")
                for h in ["Subject", "From", "To"]:
                    if msg[h]:
                        mixed[h] = msg[h]
                        # Remove from inner to avoid duplication
                        del msg[h]
                mixed.attach(msg)
                msg = mixed
            # Now msg is guaranteed to be mixed (or already mixed), attach files
            for filepath, filename in valid_attachments:
                ctype, _enc = mimetypes.guess_type(filepath)
                if ctype is None:
                    ctype = "application/octet-stream"
                maintype, subtype = ctype.split("/", 1) if "/" in ctype else ("application", "octet-stream")
                with open(filepath, "rb") as f:
                    data = f.read()
                if maintype == "text":
                    part = MIMEText(data.decode("utf-8", errors="ignore"), _subtype=subtype, _charset="utf-8")
                elif maintype == "image":
                    part = MIMEImage(data, _subtype=subtype)
                else:
                    part = MIMEBase(maintype, subtype)
                    part.set_payload(data)
                    encoders.encode_base64(part)
                if part.get("Content-Disposition"):
                    del part["Content-Disposition"]
                part.add_header("Content-Disposition", "attachment", filename=filename)
                if part.get("Content-ID"):
                    del part["Content-ID"]
                msg.attach(part)
            # Skip the old per-attachment loop below (we already handled)
            # To avoid double-processing, clear attachments so the for loop below does nothing
            attachments = []
        # Fallback for case where no valid attachments were found but original list had entries - do nothing
        # The old loop below will handle any remaining (should be none)
        for att in attachments:
            filepath = None
            filename = None
            if isinstance(att, dict):
                filepath = att.get("filepath") or att.get("path")
                filename = att.get("filename") or att.get("original") or (os.path.basename(filepath) if filepath else None)
                # If only url provided like /static/uploads/xxx.pdf
                url = att.get("url", "")
                if not filepath and url:
                    # extract filename from url
                    m2 = re.search(r'/static/uploads/([^?#"\']+)', url)
                    if m2:
                        fn = os.path.basename(m2.group(1))
                        filepath = os.path.join(os.path.dirname(__file__), "static", "uploads", fn)
                        filename = filename or fn
            elif isinstance(att, str):
                # string could be filepath or url
                if "/static/uploads/" in att:
                    m2 = re.search(r'/static/uploads/([^?#"\']+)', att)
                    if m2:
                        fn = os.path.basename(m2.group(1))
                        filepath = os.path.join(os.path.dirname(__file__), "static", "uploads", fn)
                        filename = fn
                else:
                    filepath = att
                    filename = os.path.basename(filepath)
            if not filepath or not os.path.isfile(filepath):
                continue
            filename = filename or os.path.basename(filepath)
            ctype, _enc = mimetypes.guess_type(filepath)
            if ctype is None:
                ctype = "application/octet-stream"
            maintype, subtype = ctype.split("/", 1) if "/" in ctype else ("application", "octet-stream")
            with open(filepath, "rb") as f:
                data = f.read()
            if maintype == "text":
                part = MIMEText(data.decode("utf-8", errors="ignore"), _subtype=subtype, _charset="utf-8")
            elif maintype == "image":
                part = MIMEImage(data, _subtype=subtype)
                # For generic attachments we want attachment disposition, not inline
                # So override headers later
            else:
                part = MIMEBase(maintype, subtype)
                part.set_payload(data)
                encoders.encode_base64(part)
            # Ensure attachment disposition (not inline)
            # Remove existing Content-Disposition if any (from MIMEImage)
            if part.get("Content-Disposition"):
                del part["Content-Disposition"]
            part.add_header("Content-Disposition", "attachment", filename=filename)
            # Ensure Content-ID not present for generic attachments
            if part.get("Content-ID"):
                del part["Content-ID"]
            msg.attach(part)

    # Connect with better error handling for DNS / network failures
    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
    except OSError as e:
        # Covers socket.gaierror, ConnectionRefusedError, etc.
        err = str(e)
        if "nodename nor servname" in err or "getaddrinfo" in err or "Name or service not known" in err:
            raise ConnectionError(f"SMTP sunucusu çözümlenemiyor '{host}': {err}. Sunucunun doğru olduğunu kontrol edin (örn. smtp.gmail.com, şirket adı değil) ve internet/DNS bağlantınızı kontrol edin.") from e
        raise ConnectionError(f"SMTP sunucusuna bağlanılamıyor {host}:{port}: {err}") from e

    try:
        if use_tls and not use_ssl:
            server.starttls()

        if username and password:
            try:
                server.login(username, password)
            except smtplib.SMTPAuthenticationError as e:
                raise smtplib.SMTPAuthenticationError(e.smtp_code, f"SMTP girişi başarısız: {username} on {host}:{port} — {e.smtp_error.decode() if isinstance(e.smtp_error, bytes) else e.smtp_error}. For Gmail, you must use an App Password (not your normal password): https://myaccount.google.com/apppasswords".encode() if isinstance(e.smtp_error, bytes) else f"SMTP login failed: {e}")

        server.sendmail(sender_email, [to_email], msg.as_string())
    finally:
        try:
            server.quit()
        except:
            pass
