import requests
import json
import re
import os

# --- Config handling (user can set endpoint + api_key via UI) ---
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "ai_config.json")

DEFAULT_CONFIG = {
    "provider": "openai",
    "endpoint": "https://api.openai.com/v1",
    "api_key": "",
    "model": "gpt-4o-mini"
}

def load_ai_config(user_id=None):
    # If user_id provided, load per-user from DB; else fallback to global file
    if user_id is not None:
        try:
            import sqlite3
            from database import get_db as _get_db
            conn = _get_db()
            cur = conn.cursor()
            cur.execute("SELECT provider, endpoint, api_key, model FROM ai_settings WHERE user_id=?", (user_id,))
            row = cur.fetchone()
            conn.close()
            if row:
                cfg = {**DEFAULT_CONFIG, "provider": row["provider"], "endpoint": row["endpoint"], "api_key": row["api_key"], "model": row["model"]}
                return cfg
            return {**DEFAULT_CONFIG}
        except Exception as e:
            print(f"load_ai_config user {user_id} error: {e}")
            return {**DEFAULT_CONFIG}
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                # merge with defaults
                cfg = {**DEFAULT_CONFIG, **data}
                return cfg
    except Exception as e:
        print(f"load_ai_config error: {e}")
    return {**DEFAULT_CONFIG}

def save_ai_config(provider=None, endpoint=None, api_key=None, model=None, user_id=None):
    if user_id is not None:
        # Per-user: upsert into DB
        try:
            from database import get_db as _get_db
            conn = _get_db()
            cur = conn.cursor()
            # Get existing
            cur.execute("SELECT id FROM ai_settings WHERE user_id=?", (user_id,))
            existing = cur.fetchone()
            # Load current config for merging
            cfg = load_ai_config(user_id=user_id)
            if provider is not None:
                cfg["provider"] = provider
            if endpoint is not None:
                cfg["endpoint"] = endpoint.strip().rstrip("/")
            if api_key is not None:
                cfg["api_key"] = api_key.strip()
            if model is not None:
                cfg["model"] = model.strip()
            if existing:
                cur.execute("UPDATE ai_settings SET provider=?, endpoint=?, api_key=?, model=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                            (cfg["provider"], cfg["endpoint"], cfg["api_key"], cfg["model"], user_id))
            else:
                cur.execute("INSERT INTO ai_settings (user_id, provider, endpoint, api_key, model) VALUES (?,?,?,?,?)",
                            (user_id, cfg["provider"], cfg["endpoint"], cfg["api_key"], cfg["model"]))
            conn.commit()
            conn.close()
            return cfg
        except Exception as e:
            print(f"save_ai_config user {user_id} error: {e}")
            return load_ai_config(user_id=user_id)
    cfg = load_ai_config()
    if provider is not None:
        cfg["provider"] = provider
    if endpoint is not None:
        cfg["endpoint"] = endpoint.strip().rstrip("/")
    if api_key is not None:
        cfg["api_key"] = api_key.strip()
    if model is not None:
        cfg["model"] = model.strip()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    return cfg

def get_masked_config(user_id=None):
    cfg = load_ai_config(user_id=user_id) if user_id is not None else load_ai_config()
    masked = cfg.copy()
    key = masked.get("api_key", "")
    if key and len(key) > 8:
        masked["api_key_masked"] = key[:4] + "****" + key[-4:]
        masked["has_key"] = True
    else:
        masked["api_key_masked"] = ""
        masked["has_key"] = bool(key)
    # Don't expose full key
    masked.pop("api_key", None)
    return masked

def get_company_for_user(user_id):
    if not user_id:
        return None
    try:
        from database import get_db as _get_db
        conn = _get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM company_settings WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            d = dict(row)
            # Only return if has branding and use_in_ai
            if d.get("company_name") or d.get("logo_url"):
                return d
        return None
    except Exception as e:
        print(f"get_company_for_user error: {e}")
        return None

def _company_prompt_extra(company):
    if not company:
        return ""
    parts = []
    if company.get("company_name"):
        parts.append(f"Gönderen Şirket (sender): {company['company_name']}")
    if company.get("slogan"):
        parts.append(f"Slogan: {company['slogan']}")
    if company.get("website"):
        parts.append(f"Website: {company['website']}")
    if company.get("primary_color"):
        parts.append(f"Ana Renk: {company['primary_color']}")
    if company.get("logo_url"):
        parts.append(f"Logo URL: {company['logo_url']} (HTML'de <img src=\"{company['logo_url']}\" style=\"max-height:40px\"> olarak header'da kullan)")
    if not parts:
        return ""
    return " Gönderen şirket markası: " + " | ".join(parts) + ". KRİTİK: {{Company}} ve {{Email}} ALICININ (recipient, Excel'deki kişi) şirketi/epostasıdır, gönderen şirket DEĞİLDİR. {{Company}}/{{Email}} yer tutucularını ASLA gönderen şirket ile değiştirme, koru. Gönderen şirketi sadece imzada, header logoda ve footer'da kullan."

def _recipient_vars_note():
    return " Alıcı değişkenleri (recipient, Excel'den): {{Name}} (alıcının adı), {{Company}} (ALICININ şirketi, gönderen değil), {{Email}} (alıcının e-postası), {{Amount}}, {{Month}} — hepsini koru, gönderen şirketle karıştırma."

def _should_use_branding(company):
    if not company:
        return False
    return bool(company.get("use_in_ai") == 1 and (company.get("company_name") or company.get("logo_url")))

# --- Ollama fallback (local) ---
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen2:0.5b"

def _ollama_available():
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        if r.ok:
            data = r.json()
            models = [m["name"] for m in data.get("models",[])]
            return len(models) > 0
    except:
        pass
    return False

def _get_best_ollama_model():
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        if r.ok:
            models = [m["name"] for m in r.json().get("models",[])]
            for pref in ["llama3.2:3b","llama3.1:8b","qwen2:1.5b","qwen2:7b","gemma2:2b","qwen2:0.5b"]:
                for m in models:
                    if pref in m:
                        return m
            if models:
                return models[0]
    except:
        pass
    return OLLAMA_MODEL

def _ollama_chat(system: str, user: str, temperature: float = 0.7, max_tokens: int = 800) -> str:
    model = _get_best_ollama_model()
    try:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens}
        }
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        if "message" in data and "content" in data["message"]:
            return data["message"]["content"].strip()
        if "response" in data:
            return data["response"].strip()
        return ""
    except Exception as e:
        print(f"Ollama chat error: {e}")
        return ""

# --- OpenAI-compatible API (real AI via user-configured endpoint + key) ---
def _openai_available(user_id=None):
    cfg = load_ai_config(user_id=user_id) if user_id is not None else load_ai_config()
    return bool(cfg.get("api_key") and cfg.get("endpoint"))

def _openai_chat(system: str, user: str, temperature: float = 0.7, max_tokens: int = 1000, user_id=None) -> str:
    cfg = load_ai_config(user_id=user_id) if user_id is not None else load_ai_config()
    api_key = cfg.get("api_key", "").strip()
    endpoint = cfg.get("endpoint", "").strip().rstrip("/")
    model = cfg.get("model", "gpt-4o-mini").strip() or "gpt-4o-mini"
    if not api_key or not endpoint:
        return ""
    # Normalize endpoint: ensure it ends with /chat/completions handling
    # If endpoint already contains /chat/completions, use as is, else append
    url = endpoint
    if not url.endswith("/chat/completions"):
        # Common: endpoint is https://api.openai.com/v1 -> need /chat/completions
        if url.endswith("/v1"):
            url = url + "/chat/completions"
        elif "/v1" not in url:
            url = url + "/v1/chat/completions"
        else:
            url = url + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ],
        "temperature": temperature,
        "max_tokens": max_tokens
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        if not r.ok:
            # Try to parse error
            try:
                err = r.json()
                print(f"OpenAI API error {r.status_code}: {err}")
            except:
                print(f"OpenAI API error {r.status_code}: {r.text[:500]}")
            return ""
        data = r.json()
        # OpenAI format: choices[0].message.content
        if "choices" in data and len(data["choices"]) > 0:
            msg = data["choices"][0].get("message", {})
            content = msg.get("content", "")
            if content:
                return content.strip()
        # Some providers return different format
        if "response" in data:
            return str(data["response"]).strip()
        return ""
    except Exception as e:
        print(f"OpenAI chat error: {e}")
        return ""

def _real_chat(system: str, user: str, temperature: float = 0.7, max_tokens: int = 800, user_id=None) -> str:
    # Try OpenAI first (user-configured real AI), then Ollama, then fail
    # OpenAI is preferred for quality and Turkish support
    if _openai_available(user_id=user_id):
        resp = _openai_chat(system, user, temperature, max_tokens, user_id=user_id)
        if resp and len(resp.strip()) > 10:
            return resp
        # If OpenAI returned empty or too short, try Ollama as fallback
    if _ollama_available():
        resp = _ollama_chat(system, user, temperature, max_tokens)
        if resp:
            return resp
    return ""

# Public API used by main.py
def real_ai_enhance_subject(text: str, user_id=None) -> str:
    company = get_company_for_user(user_id) if user_id else None
    extra = _company_prompt_extra(company) if _should_use_branding(company) else ""
    recipient_note = _recipient_vars_note()
    # Try real AI
    system = "Sen Mailium için e-posta konu satırı iyileştiren bir asistansın. Türkçe, profesyonel, kısa (30-60 karakter), {{Name}} gibi yer tutucuları KESİNLİKLE koru ve değiştirme. Sadece konuyu iyileştir, açıklama ekleme, sadece iyileştirilmiş konuyu döndür." + recipient_note + extra
    user = f"Şu konu satırını iyileştir, daha etkili ve profesyonel yap, Türkçe kal, yer tutucuları koru:\n\n\"{text}\"\n\nSadece iyileştirilmiş konu satırını döndür, tırnak ekleme."
    resp = _real_chat(system, user, temperature=0.6, max_tokens=120, user_id=user_id)
    if resp:
        resp = resp.strip().strip('"').strip("'").split("\n")[0].strip()
        if "{{" in text and "{{" not in resp:
            return ""  # placeholder lost, fallback
        if len(resp) > 100:
            resp = resp[:100]
        if len(resp) < 5:
            return ""
        return resp
    return ""

def real_ai_enhance_body(html_or_text: str, user_id=None) -> str:
    company = get_company_for_user(user_id) if user_id else None
    brand_extra = _company_prompt_extra(company) if _should_use_branding(company) else " Şirket adını kullanma, nötr kal."
    recipient_note = _recipient_vars_note()
    system = """Sen Mailium için e-posta gövdesi iyileştiren bir asistansın. Türkçe, profesyonel, samimi ve kurumsal bir dil kullan. 
- Merhaba {{Name}} gibi selamlama ve nötr bir kapanış (Saygılarımızla,) ekle eğer yoksa
- {{Name}}, {{Company}}, {{Email}} gibi yer tutucuları KESİNLİKLE koru, asla silme veya değiştirme — {{Company}}/{{Email}} alıcının bilgisidir, gönderen şirketle karıştırma
- Paragrafları <p> etiketleriyle ayır, önemli yerleri <strong> ile vurgula
- Sadece iyileştirilmiş HTML gövdesini döndür, açıklama ekleme, <html> sarmalama yapma, sadece <p> blokları
""" + recipient_note + brand_extra
    user = f"Şu e-posta gövdesini iyileştir, daha profesyonel ve akıcı Türkçe yap, yer tutucuları koru:\n\n{html_or_text}\n\nSadece iyileştirilmiş HTML'i döndür:"
    resp = _real_chat(system, user, temperature=0.7, max_tokens=900, user_id=user_id)
    if resp:
        if "<p" not in resp.lower():
            resp = f"<p>{resp}</p>"
        if "{{Name}}" in html_or_text and "{{Name}}" not in resp:
            resp = f"<p>Merhaba {{{{Name}}}},</p>" + resp
        # Basic quality check: should not be too short or repetitive nonsense
        if len(resp) < 20:
            return ""
        return resp.strip()
    return ""

def real_ai_generate_fancy_html(content: str, subject: str = "", template: str = "modern", user_id=None) -> str:
    company = get_company_for_user(user_id) if user_id else None
    brand_extra = _company_prompt_extra(company) if _should_use_branding(company) else " Şirket adını kullanma, nötr kal, header'da sadece konu ve genel bir başlık kullan."
    recipient_note = _recipient_vars_note()
    logo_instruction = ""
    if company and company.get("logo_url") and _should_use_branding(company):
        logo_instruction = f" Header'da logoyu <img src=\"{company['logo_url']}\" alt=\"{company.get('company_name','Logo')}\" style=\"max-height:40px; max-width:120px;\"> olarak ekle. {{Company}} alıcının şirketidir, gönderen şirketle karıştırma."
    template_styles = {
        "modern": "Modern kurumsal: header #4f46e5, beyaz gövde, 600px, yuvarlak CTA butonu #4f46e5",
        "minimal": "Minimal temiz: beyaz header siyah logo, çok beyaz alan, ince gri çizgiler, siyah CTA butonu #111827",
        "bold": "Bold koyu: header #0f172a, gövde beyaz, vurgu renk #f59e0b, kalın tipografi",
        "elegant": "Elegant zarif: header #ffffff, serif başlık, bej arka plan #fefce8, ince altın çizgi #eab308, merkez hizalı",
        "newsletter": "Newsletter: üstte kategori etiketi, 2 kolon yok sadece tek kolon, görsel alan placeholder, okunabilir makale stili"
    }
    style_desc = template_styles.get(template, template_styles["modern"])
    system = f"""Sen Mailium için E-POSTA İSTEMCİ UYUMLU şık HTML e-posta şablonları oluşturan bir tasarımcısın.
KRİTİK KURALLAR (e-posta istemcileri için):
- Sadece TABLO tabanlı (table/tr/td) kullan, div ile max-width değil; Outlook/Gmail uyumlu
- Tüm CSS INLINE olmalı, <style> veya external CSS kullanma, gradient (linear-gradient) ASLA kullanma (Outlook desteklemez)
- Font olarak Arial, Helvetica, sans-serif kullan
- Genişlik 600px, border 1px solid #e2e8f0, padding 32px
- CTA butonu için <a> içinde inline background, border-radius, padding
 - Sadece tablo HTML'ini döndür, açıklama, markdown fence (```) ekleme, sadece HTML kodu
- {{{{Name}}}}, {{{{Company}}}} yer tutucularını KESİNLİKLE koru — {{{{Company}}}} ALICININ şirketidir, gönderen şirket ({company.get('company_name') if company and company.get('company_name') else 'yok'}) ile karıştırma
- {brand_extra}{logo_instruction}{recipient_note}
- Seçilen stil: {template} — {style_desc}
"""
    user = f"Konu: {subject or 'Mailium Bilgilendirme'}\nStil: {template}\n\nİçerik (yer tutucuları koru):\n{content}\n\nBu içerikten şık, responsive inline-CSS HTML e-posta şablonu oluştur. Sadece HTML'i döndür:"
    resp = _real_chat(system, user, temperature=0.7, max_tokens=1800, user_id=user_id)
    if resp:
        # Strip markdown fences if LLM wrapped in ```html
        resp = re.sub(r'^```(?:html)?\s*', '', resp.strip(), flags=re.I)
        resp = re.sub(r'\s*```$', '', resp.strip())
        # Ensure it looks like email HTML (table or div)
        if "<table" in resp.lower() or "<div" in resp.lower():
            # Ensure placeholders still present if original had them
            if "{{Name}}" in content and "{{Name}}" not in resp:
                # Heuristic fallback will handle, return empty to trigger fallback
                return ""
            return resp.strip()
    return ""

def real_ai_compose(prompt: str, tone: str = "professional", language: str = "tr", extra_context: str = "", user_id=None) -> dict:
    """Compose subject + body from a short prompt. Returns dict with subject, body."""
    company = get_company_for_user(user_id) if user_id else None
    brand_extra = _company_prompt_extra(company) if _should_use_branding(company) else " Şirket adını kullanma, nötr imza kullan."
    closing = f"Saygılarımızla,<br><strong>{company['company_name']}</strong>" if _should_use_branding(company) and company.get("company_name") else "Saygılarımızla,"
    tone_map = {
        "professional": "profesyonel kurumsal, saygılı",
        "friendly": "samimi sıcak, dostane",
        "formal": "çok resmi, ciddi",
        "casual": "rahat gündelik, kısa",
        "persuasive": "ikna edici pazarlama odaklı, CTA vurgulu"
    }
    tone_desc = tone_map.get(tone, tone_map["professional"])
    recipient_note = _recipient_vars_note()
    system = f"""Sen Mailium için e-posta yazan profesyonel bir asistanın. Dil: {language}, Ton: {tone_desc}.
GÖREV: Kullanıcının kısa talimatından tam e-posta oluştur.
KURALLAR:
- Konu satırı 35-65 karakter, Türkçe, etkili
- Gövde: <p> etiketleriyle paragraflar, önemli yerler <strong>, selamlama Merhaba {{{{Name}}}}, ve kapanış {closing} ile bitir
- {{{{Name}}}}, {{{{Company}}}}, {{{{Email}}}}, {{{{Amount}}}}, {{{{Month}}}} gibi yer tutucuları KORU ve uygun yerlerde KULLAN (en az {{{{Name}}}} ve {{{{Company}}}} ekle) — {{{{Company}}}}/{{{{Email}}}} ALICININ bilgileridir, gönderen şirket ({company.get('company_name') if company and company.get('company_name') else 'yok'}) ile karıştırma
- {brand_extra} {recipient_note}
- Sadece JSON döndür: {{"subject": "...", "body": "<p>...</p>"}} — başka açıklama ekleme, markdown yok
"""
    company_ctx = f" Şirket: {company['company_name']}" if company and company.get("company_name") else ""
    user = f"Talimat: {prompt}\nEk bağlam: {extra_context or '-'}{company_ctx}\n\nBu talimattan konu ve gövde oluştur, JSON döndür."
    resp = _real_chat(system, user, temperature=0.75, max_tokens=1200, user_id=user_id)
    if resp:
        # Try to parse JSON
        cleaned = re.sub(r'^```(?:json)?\s*', '', resp.strip(), flags=re.I)
        cleaned = re.sub(r'\s*```$', '', cleaned.strip())
        try:
            # Extract JSON object
            m = re.search(r'\{.*\}', cleaned, re.S)
            if m:
                data = json.loads(m.group(0))
                subj = str(data.get("subject", "")).strip().strip('"').strip("'")
                body = str(data.get("body", "")).strip()
                if not subj or not body:
                    return {}
                if "<p" not in body.lower():
                    body = f"<p>{body}</p>"
                # Ensure placeholders
                if "{{Name}}" not in body:
                    body = f"<p>Merhaba {{{{Name}}}},</p>" + body
                return {"subject": subj[:120], "body": body}
        except Exception as e:
            print(f"compose parse error: {e} resp={resp[:300]}")
        # Fallback: try to split response as subject/body lines
        lines = [l.strip() for l in cleaned.split("\n") if l.strip()]
        if lines:
            subj = lines[0].replace("Subject:", "").replace("Konu:", "").strip()[:80]
            body_text = "\n".join(lines[1:]) if len(lines) > 1 else cleaned
            if body_text:
                if "<p" not in body_text.lower():
                    body_text = f"<p>{body_text}</p>"
                return {"subject": subj, "body": body_text}
    return {}

def real_ai_fix_grammar(text: str, field: str = "body", user_id=None) -> str:
    """Fix grammar / spelling without changing meaning or placeholders."""
    if not text or not text.strip():
        return ""
    if field == "subject":
        system = """Sen Türkçe konu satırı dilbilgisi düzeltme asistanısın. Sadece yazım, noktalama, büyük/küçük harf ve akıcılığı düzelt. Anlamı değiştirme, uzatma veya kısaltma yapma, {{{{Name}}}} yer tutucularını KESİNLİKLE koru. Sadece düzeltilmiş konu satırını döndür, açıklama ekleme."""
        user = f"Şu konu satırının dilbilgisini düzelt, sadece düzeltilmiş hali:\n\n\"{text}\""
        resp = _real_chat(system, user, temperature=0.2, max_tokens=150, user_id=user_id)
        if resp:
            resp = resp.strip().strip('"').strip("'").split("\n")[0].strip()
            if "{{" in text and "{{" not in resp:
                return ""
            return resp
        return ""
    else:
        # body — may be HTML
        has_html = "<p" in text.lower() or "<div" in text.lower() or "<br" in text.lower()
        system = """Sen Türkçe e-posta gövdesi dilbilgisi ve yazım düzeltme asistanısın.
- Sadece yazım hatalarını, noktalama, büyük/küçük harf, eklerin yazımını ve akıcılığı düzelt
- Anlamı ve üslubu KORU, yeni cümle ekleme, kısaltma yapma
- HTML etiketlerini (<p>, <strong>, <a>, <br>) ve {{{{Name}}}}, {{{{Company}}}} yer tutucularını KESİNLİKLE koru
- Sadece düzeltilmiş HTML gövdesini döndür, açıklama ekleme
"""
        user = f"Şu e-posta gövdesinin dilbilgisi ve yazımını düzelt, sadece düzeltilmiş HTML'i döndür:\n\n{text}"
        resp = _real_chat(system, user, temperature=0.2, max_tokens=1500, user_id=user_id)
        if resp:
            cleaned = re.sub(r'^```(?:html)?\s*', '', resp.strip(), flags=re.I)
            cleaned = re.sub(r'\s*```$', '', cleaned.strip())
            if "{{Name}}" in text and "{{Name}}" not in cleaned:
                return ""
            if len(cleaned.strip()) < 10:
                return ""
            return cleaned.strip()
        return ""

def real_ai_chat(message: str, history: list = None, user_id=None) -> str:
    system = """Sen Mailium E-posta Otomasyon Stüdyosu'nun yardımsever yapay zeka asistanısın. Türkçe konuş.
Görevlerin:
- Sistemde gezinmeye yardım et: Kontrol Paneli, Yeni Kampanya (3 adım: Alıcılar → Mesaj → Zamanlama), Kampanyalar, Kayıtlar, SMTP Ayarları, Ayarlar
- Uygulama Şifresi (App Password) alma konusunda adım adım rehberlik et (Gmail: myaccount.google.com → Güvenlik → 2 Adımlı Doğrulama → Uygulama şifreleri → smtp.gmail.com:587 TLS)
- Zamanlama, Excel, değişkenler {{Name}}, dosya ekleri, görsel gömme konularında yardım et
- Kısa, net, madde işaretli cevaplar ver, gerektiğinde link ekle
- Asla yer tutucuları silme, Türkçe kal
- Yazılarda şirket adını kullanma isteğine saygı duy
"""
    user = message
    if history:
        hist_text = "\n".join([f"Kullanıcı: {h.get('user','')} \nAsistan: {h.get('assistant','')}" for h in history[-3:]])
        user = f"Geçmiş:\n{hist_text}\n\nYeni soru: {message}"
    resp = _real_chat(system, user, temperature=0.6, max_tokens=800, user_id=user_id)
    if resp:
        return resp.strip()
    return ""

def get_ai_status(user_id=None):
    cfg = load_ai_config(user_id=user_id) if user_id is not None else load_ai_config()
    has_openai = bool(cfg.get("api_key"))
    ollama_avail = _ollama_available()
    model_info = cfg.get("model") if has_openai else (_get_best_ollama_model() if ollama_avail else None)
    provider = "Kapalı (heuristic)"
    real = False
    url = cfg.get("endpoint", "")
    if has_openai:
        provider = f"OpenAI Uyumlu ({model_info})"
        real = True
        url = cfg.get("endpoint")
    elif ollama_avail:
        provider = f"Ollama Yerel ({model_info})"
        real = True
        url = OLLAMA_URL
    return {
        "real_ai": real,
        "model": model_info,
        "provider": provider,
        "url": url,
        "has_key": has_openai,
        "ollama_available": ollama_avail,
        "config": get_masked_config(user_id=user_id) if user_id is not None else get_masked_config()
    }
