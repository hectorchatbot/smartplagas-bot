# -*- coding: utf-8 -*-
from dotenv import load_dotenv
load_dotenv(override=True)

import os, re, time, unicodedata, datetime, json
import subprocess, shutil  # <-- para LibreOffice
from flask import Flask, request, jsonify, send_from_directory, abort
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

# --- DOCX (plantilla Jinja) ---
from docxtpl import DocxTemplate

# Intento 1: Word/docx2pdf (Windows/Mac)
try:
    from docx2pdf import convert as docx2pdf_convert
except Exception:
    docx2pdf_convert = None

# COM para Word (evita "No se ha llamado a CoInitialize" en Windows)
try:
    import pythoncom
except Exception:
    pythoncom = None

app = Flask(__name__)

# --- CORS (para Botpress/Browser fetch) ---
ALLOWED_ORIGIN  = os.getenv("CORS_ALLOW_ORIGIN", "*")
ALLOWED_METHODS = "GET, POST, OPTIONS"
ALLOWED_HEADERS = "Content-Type, ngrok-skip-browser-warning, Authorization"

@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"]  = ALLOWED_ORIGIN
    resp.headers["Access-Control-Allow-Methods"] = ALLOWED_METHODS
    resp.headers["Access-Control-Allow-Headers"] = ALLOWED_HEADERS
    return resp

def _cors_preflight():
    return ("", 204, {
        "Access-Control-Allow-Origin":  ALLOWED_ORIGIN,
        "Access-Control-Allow-Methods": ALLOWED_METHODS,
        "Access-Control-Allow-Headers": ALLOWED_HEADERS,
    })

# ========== Config ==========
TW_SID   = os.getenv("TWILIO_ACCOUNT_SID", "")
TW_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TW_FROM  = os.getenv("TWILIO_WHATSAPP_FROM") or os.getenv("TWILIO_PHONE_NUMBER") or "whatsapp:+14155238886"
ADMIN_WA = os.getenv("ADMIN_WHATSAPP") or os.getenv("MY_PHONE_NUMBER") or ""

BASE_URL = (os.getenv("BASE_URL") or os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")

# Archivos públicos SIEMPRE en out/
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
FILES_SUBDIR = (os.getenv("FILES_DIR", "out") or "out").strip()
FILES_DIR    = os.path.join(BASE_DIR, FILES_SUBDIR)
os.makedirs(FILES_DIR, exist_ok=True)

# Plantilla fija (NO cambiar)
TEMPLATE_DOCX = os.getenv("TEMPLATE_DOCX", os.path.join(BASE_DIR, "templates", "templatescotizacion_template.docx"))

# Envíos
SEND_PDF    = (os.getenv("SEND_PDF_TO_CLIENT", "true").lower() == "true")
SEND_DOC    = (os.getenv("SEND_DOC_TO_CLIENT", "false").lower() == "true")
MEDIA_DELAY = float(os.getenv("MEDIA_DELAY_SECONDS", "1.0"))

# Twilio client
twilio = Client(TW_SID, TW_TOKEN) if (TW_SID and TW_TOKEN) else None


# ========== Precios por rangos (CLP) ==========
TRAMOS = [
    (0, 50), (51, 100), (101, 200), (201, 300),
    (301, 500), (501, 1000), (1001, 2000), (2001, 9999999),
]
PRECIOS = {
    "desinsectacion": [ 37500,  47500,  65000,  80000, 105000, 165000, 270000, 440000 ],
    "desratizacion":  [ 34000,  44000,  60000,  75000,  97500, 150000, 235000, 375000 ],
    "desinfeccion":   [ 30000,  40000,  55000,  70000,  90000, 140000, 220000, 350000 ],
}
def _fmt_money_clp(value: int) -> str:
    return f"${value:,}".replace(",", ".")

def _strip_accents_and_symbols(text: str) -> str:
    t = text or ""
    t = re.sub(r"[\u2460-\u24FF\u2600-\u27BF\ufe0f\u200d]", "", t)
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    return re.sub(r"[^a-zA-Z0-9\s]", " ", t).lower().strip()

def _canon_servicio(servicio: str) -> str:
    s = _strip_accents_and_symbols(servicio)
    if "desinsect" in s:  return "desinsectacion"
    if "desratiz"  in s:  return "desratizacion"
    if "desinfect" in s:  return "desinfeccion"
    return "desinsectacion"

def precio_por_tramo(servicio: str, m2: float) -> int:
    tabla = PRECIOS.get(_canon_servicio(servicio))
    if not tabla: return 0
    m2n = int(float(m2) if m2 else 0)
    for idx, (lo, hi) in enumerate(TRAMOS):
        if lo <= m2n <= hi:
            return int(tabla[idx])
    return int(tabla[-1])


# ========== Utilidades JSON / URLs ==========
def _safe(x):
    if x is None: return ""
    if isinstance(x, (list, tuple)):
        return ", ".join(_safe(v) for v in x)
    if isinstance(x, dict):
        for k in ("label", "title", "name", "value", "text"):
            if k in x and x[k] not in (None, ""):
                return _safe(x[k])
        return ""
    return str(x).strip()

def public_base_from_request():
    if BASE_URL: return BASE_URL
    proto = request.headers.get("X-Forwarded-Proto", "https")
    host  = request.headers.get("X-Forwarded-Host", request.host)
    return f"{proto}://{host}"

def build_urls(filename_docx: str, filename_pdf: str):
    public = public_base_from_request().rstrip("/")
    docx_url = f"{public}/files/{filename_docx}"
    pdf_url  = f"{public}/files/{filename_pdf}"

    def _bypass(u: str) -> str:
        if "ngrok-free.app" in u and "ngrok-skip-browser-warning" not in u:
            sep = "&" if "?" in u else "?"
            return f"{u}{sep}ngrok-skip-browser-warning=true"
        return u

    return _bypass(docx_url), _bypass(pdf_url)


# ========== Normalización del payload ==========
def normalize_payload(data: dict) -> dict:
    data = data or {}
    servicio  = _safe(data.get("servicioinicial") or data.get("servicio") or data.get("servicio_inicial"))
    cliente   = _safe(data.get("tipo_clientes")   or data.get("cliente")  or data.get("tipo_cliente"))
    m2_raw    = _safe(data.get("metro_2")         or data.get("m2")       or data.get("metros2"))
    direccion = _safe(data.get("lugar_D")         or data.get("direccion") or data.get("ubicacion"))
    comuna    = _safe(data.get("comuna"))
    detalles  = _safe(data.get("detalles_A")      or data.get("detalles"))
    contacto  = _safe(data.get("nomape_A")        or data.get("contacto")  or data.get("nombre"))
    fono      = _safe(data.get("fono")            or data.get("telefono")  or data.get("phone"))
    email     = _safe(data.get("correoelect")     or data.get("email"))

    try:
        m2_num = float((m2_raw or "0").lower().replace("m2","").replace("m²","").replace(",",".").strip() or "0")
    except Exception:
        m2_num = 0.0

    to_wa = ""
    if fono:
        digits = "".join(ch for ch in fono if ch.isdigit())
        if   digits.startswith("56"): to_wa = f"whatsapp:+{digits}"
        elif len(digits) == 9:        to_wa = f"whatsapp:+56{digits}"
        elif digits:                  to_wa = f"whatsapp:+{digits}"

    return {
        "fecha": datetime.date.today().strftime("%d-%m-%Y"),
        "servicio": servicio,
        "cliente": cliente,
        "m2": m2_num,
        "direccion": direccion,
        "comuna": comuna,
        "detalles": detalles,
        "contacto": contacto,
        "email": email,
        "to_whatsapp": to_wa
    }


# ========== Generación con PLANTILLA ==========
def generar_docx_desde_plantilla(path: str, info: dict) -> None:
    if not os.path.exists(TEMPLATE_DOCX):
        raise FileNotFoundError(f"Plantilla no encontrada: {TEMPLATE_DOCX}")

    tpl = DocxTemplate(TEMPLATE_DOCX)
    try:
        m2_txt = str(int(info["m2"])) if float(info["m2"]).is_integer() else str(info["m2"])
    except Exception:
        m2_txt = str(info["m2"])

    total = precio_por_tramo(info["servicio"], info["m2"])
    context = {
        "fecha":     info["fecha"],
        "cliente":   info["cliente"],
        "direccion": info["direccion"],
        "comuna":    info.get("comuna",""),
        "contacto":  info["contacto"],
        "email":     info["email"],
        "servicio":  info["servicio"],
        "m2":        m2_txt,
        "precio":    _fmt_money_clp(total),
    }
    tpl.render(context)
    tpl.save(path)

# ---------- Conversión a PDF: Word (docx2pdf) o LibreOffice ----------
def _lo_bin():
    """Devuelve 'soffice' o 'libreoffice' si existe en PATH; si no, None."""
    for name in ("soffice", "libreoffice"):
        if shutil.which(name):
            return name
    return None

def convertir_docx_a_pdf_con_lo(docx_path: str, pdf_path: str) -> None:
    """DOCX -> PDF usando LibreOffice headless (Linux)."""
    outdir = os.path.dirname(pdf_path)
    bin_lo = _lo_bin()
    if not bin_lo:
        raise RuntimeError("LibreOffice no está disponible en el contenedor.")
    cmd = [bin_lo, "--headless", "--convert-to", "pdf", "--outdir", outdir, docx_path]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    base_pdf  = os.path.splitext(os.path.basename(docx_path))[0] + ".pdf"
    generated = os.path.join(outdir, base_pdf)
    if os.path.exists(generated) and generated != pdf_path:
        os.replace(generated, pdf_path)
    if not os.path.exists(pdf_path):
        raise RuntimeError("LibreOffice no generó el PDF")

def convertir_docx_a_pdf(docx_path: str, pdf_path: str) -> None:
    """
    1) Si docx2pdf (Word) está disponible (Windows/Mac), úsalo.
    2) Si no, usa LibreOffice headless (Linux).
    """
    # Ruta 1: Word/docx2pdf
    if docx2pdf_convert is not None:
        time.sleep(0.4)
        com_inicializado = False
        try:
            if pythoncom is not None:
                try:
                    pythoncom.CoInitialize()
                    com_inicializado = True
                except Exception:
                    pass
            docx2pdf_convert(docx_path, pdf_path)
        finally:
            if com_inicializado:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
        if not os.path.exists(pdf_path):
            raise RuntimeError("No se generó el PDF (docx2pdf).")
        return

    # Ruta 2: LibreOffice headless
    convertir_docx_a_pdf_con_lo(docx_path, pdf_path)


# ========== WhatsApp ==========
def send_whatsapp_media_only_pdf(to_wa: str, caption: str, pdf_url: str, delay: float = MEDIA_DELAY):
    result = {}
    if not (twilio and to_wa and pdf_url):
        result["warn"] = "twilio_or_params_missing"
        return result
    try:
        time.sleep(max(0.0, delay))
        msg = twilio.messages.create(
            from_=TW_FROM,
            to=to_wa,
            body=caption,
            media_url=[pdf_url]
        )
        result["single_msg_sid"] = msg.sid
    except Exception as e:
        result["error"] = str(e)
    return result


# ========== Archivos públicos ==========
@app.route("/files/<path:filename>")
def files_route(filename):
    full = os.path.abspath(os.path.join(FILES_DIR, filename))
    base = os.path.abspath(FILES_DIR)
    if not full.startswith(base): abort(403)
    if not os.path.exists(full): abort(404)
    return send_from_directory(FILES_DIR, filename, as_attachment=False)

@app.route("/static/<path:filename>")
def static_files(filename):
    return files_route(filename)


# ========== Health ==========
@app.get("/health")
def health():
    lo_ok = bool(_lo_bin())
    engine = "docx2pdf" if docx2pdf_convert is not None else ("libreoffice" if lo_ok else "none")
    return jsonify({
        "status": "ok",
        "base_url": public_base_from_request(),
        "files_dir": FILES_DIR,
        "template_exists": os.path.exists(TEMPLATE_DOCX),
        "pdf_engine": engine,
        "send_pdf": SEND_PDF,
        "send_doc": SEND_DOC,
        "media_delay": MEDIA_DELAY,
        "twilio_from": TW_FROM,
    }), 200


# ========== Lectura de payload tolerante ==========
def _read_payload_any():
    if request.is_json:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            return data
    try:
        raw = (request.data or b"").decode("utf-8").strip()
        if raw:
            j = json.loads(raw)
            if isinstance(j, dict):
                return j
    except Exception:
        pass
    if request.form:
        return {k: v for k, v in request.form.items()}
    return {}


# ========== Core (JSON externo: Botpress, Postman, etc.) ==========
def handle_generate():
    payload = _read_payload_any()
    info = normalize_payload(payload)

    faltantes = [k for k in ("servicio","cliente","m2","direccion","contacto") if not info[k]]
    if faltantes:
        return jsonify(ok=True, message="Campos mínimos faltantes; no se generan archivos",
                       missing=faltantes, received=payload), 200

    if not os.path.exists(TEMPLATE_DOCX):
        return jsonify(ok=False, error="template_missing",
                       detail=f"No se encontró la plantilla: {TEMPLATE_DOCX}"), 500

    # Verificamos que exista al menos UNA vía de conversión a PDF
    if (docx2pdf_convert is None) and (not _lo_bin()):
        return jsonify(ok=False, error="pdf_engine_missing",
                       detail="No hay Word/docx2pdf ni LibreOffice disponibles para convertir a PDF."), 500

    # 3) nombres/paths
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"cotizacion_{ts}"
    docx_name = base + ".docx"
    pdf_name  = base + ".pdf"
    docx_path = os.path.join(FILES_DIR, docx_name)
    pdf_path  = os.path.join(FILES_DIR, pdf_name)

    # 4) DOCX (plantilla SIEMPRE)
    try:
        generar_docx_desde_plantilla(docx_path, info)
    except Exception as e:
        return jsonify(ok=False, error="template_render_failed", detail=str(e)), 500

    # 5) PDF con Word o LibreOffice
    try:
        convertir_docx_a_pdf(docx_path, pdf_path)
    except Exception as e:
        return jsonify(ok=False, error="pdf_convert_failed", detail=str(e)), 500

    # 6) URLs públicas
    docx_url, pdf_url = build_urls(docx_name, pdf_name)

    # 7) Resumen WA (incluye total)
    total = _fmt_money_clp(precio_por_tramo(info["servicio"], info["m2"]))
    partes = [
        "✅ *Nueva solicitud recibida*\n",
        f"*Servicio:* {info['servicio']}\n",
        f"*Cliente:* {info['cliente']}\n",
        f"*Metros²:* {info['m2']}\n",
        f"*Ubicación:* {info['direccion']}\n",
    ]
    if info.get("comuna"):
        partes.append(f"*Comuna:* {info['comuna']}\n")
    partes.extend([
        f"*Detalles:* {info['detalles']}\n",
        f"*Contacto:* {info['contacto']} | {info['email']}\n",
        f"*Total:* {total}"
    ])
    resumen = "".join(partes)

    # 8) WhatsApp (SOLO PDF)
    sids = {}
    if info["to_whatsapp"] and SEND_PDF:
        sids["client"] = send_whatsapp_media_only_pdf(info["to_whatsapp"], "📎 Cotización adjunta", pdf_url, MEDIA_DELAY)
    if ADMIN_WA and SEND_PDF:
        sids["admin"]  = send_whatsapp_media_only_pdf(ADMIN_WA, "📣 Copia interna\n\n" + resumen, pdf_url, MEDIA_DELAY)

    return jsonify(ok=True,
                   resumen=resumen,
                   docx_url=docx_url,
                   pdf_url=pdf_url,
                   to_wa=info["to_whatsapp"],
                   twilio=sids), 200


# ---- Rutas JSON con CORS ----
@app.route("/gen-cotizacion", methods=["POST", "OPTIONS"])
def gen_cotizacion():
    if request.method == "OPTIONS":
        return _cors_preflight()
    return handle_generate()

@app.route("/webhook", methods=["POST", "OPTIONS"])
def webhook():
    if request.method == "OPTIONS":
        return _cors_preflight()
    return handle_generate()


# ================== PARSEO DE MENSAJES ENTRANTES (WhatsApp) ==================
CAMPO_ALIASES = {
    "servicio": ["servicio", "servicioinicial", "tipo_servicio"],
    "m2": ["m2", "metro_2", "metros2", "metros", "superficie", "mt2", "mts2"],
    "direccion": ["direccion", "lugar_d", "ubicacion", "dirección"],
    "comuna": ["comuna"],
    "cliente": ["cliente", "tipo_clientes", "tipo_cliente"],
    "detalles": ["detalles", "detalles_a", "nota", "comentario"],
    "contacto": ["contacto", "nombre", "nomape_a"],
    "email": ["email", "correo", "correoelect", "mail"],
}

def _kv_scan(text: str) -> dict:
    out = {}
    text = text.replace("\r", "\n")
    parts = re.split(r"[;\n]+", text)
    for p in parts:
        if not p.strip():
            continue
        m = re.match(r"\s*([a-zA-Z_áéíóúñ]+)\s*[:=]\s*(.+)$", p.strip())
        if not m:
            continue
        k_raw, v = m.group(1).strip(), m.group(2).strip()
        k = k_raw.lower()
        for canon, aliases in CAMPO_ALIASES.items():
            if k in aliases:
                out[canon] = v
                break
    return out

def _infer_from_free_text(text: str) -> dict:
    info = {}
    s = text.lower()

    if "desinsect" in s:  info["servicio"] = "Desinsectación interior"
    elif "desratiz" in s: info["servicio"] = "Desratización"
    elif "desinfect" in s:info["servicio"] = "Desinfección"

    m = re.search(r"(\d{2,5})\s*(m2|m²|mts2|mt2)?", s)
    if m:
        info["m2"] = m.group(1)

    m = re.search(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", text, re.I)
    if m:
        info["email"] = m.group(0)

    m = re.search(r"(?:dir(?:ecci[oó]n)?|ubicaci[oó]n)\s*[:\-]?\s*(.+)", s)
    if m:
        info["direccion"] = m.group(1).strip()

    m = re.search(r"(?:soy|me llamo|nombre)\s*[:\-]?\s*([a-záéíóúñ ]{3,})", s, re.I)
    if m:
        info["contacto"] = m.group(1).strip().title()

    return info

def parse_inbound_payload(body_text: str) -> dict:
    body_text = body_text or ""
    data = {}
    data.update(_kv_scan(body_text))
    inferred = _infer_from_free_text(body_text)
    for k, v in inferred.items():
        data.setdefault(k, v)
    data.setdefault("cliente", "Residencial")
    data.setdefault("detalles", "")
    return data

def generar_y_enviar_desde_info(info_in: dict):
    required = ("servicio","cliente","m2","direccion","contacto")
    faltan = [k for k in required if not info_in.get(k)]
    if faltan:
        return (False, "", None, f"faltan_campos:{','.join(faltan)}")

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"cotizacion_{ts}"
    docx_name = base + ".docx"
    pdf_name  = base + ".pdf"
    docx_path = os.path.join(FILES_DIR, docx_name)
    pdf_path  = os.path.join(FILES_DIR, pdf_name)

    try:
        generar_docx_desde_plantilla(docx_path, info_in)
    except Exception as e:
        return (False, "", None, f"template_render_failed:{e}")

    try:
        convertir_docx_a_pdf(docx_path, pdf_path)
    except Exception as e:
        return (False, "", None, f"pdf_convert_failed:{e}")

    docx_url, pdf_url = build_urls(docx_name, pdf_name)

    total = _fmt_money_clp(precio_por_tramo(info_in["servicio"], info_in["m2"]))
    partes = [
        "✅ *Nueva cotización generada*\n",
        f"*Servicio:* {info_in['servicio']}\n",
        f"*Cliente:* {info_in['cliente']}\n",
        f"*Metros²:* {info_in['m2']}\n",
        f"*Ubicación:* {info_in['direccion']}\n",
    ]
    if info_in.get("comuna"):
        partes.append(f"*Comuna:* {info_in['comuna']}\n")
    partes.extend([
        f"*Detalles:* {info_in.get('detalles','')}\n",
        f"*Contacto:* {info_in['contacto']} | {info_in.get('email','')}\n",
        f"*Total:* {total}"
    ])
    resumen = "".join(partes)

    sids = {}
    if info_in.get("to_whatsapp") and SEND_PDF:
        sids["client"] = send_whatsapp_media_only_pdf(info_in["to_whatsapp"], "📎 Cotización adjunta", pdf_url, MEDIA_DELAY)
    if ADMIN_WA and SEND_PDF:
        sids["admin"]  = send_whatsapp_media_only_pdf(ADMIN_WA, "📣 Copia interna\n\n" + resumen, pdf_url, MEDIA_DELAY)

    return (True, resumen, {"docx": docx_url, "pdf": pdf_url}, None)


# ========== Webhook Twilio ==========
@app.post("/twilio-inbound")
def twilio_inbound():
    from_wa = (request.form.get("From", "") or "").strip()
    body    = (request.form.get("Body", "") or "").strip()

    parsed = parse_inbound_payload(body)

    parsed["fono"] = from_wa.replace("whatsapp:", "") if from_wa.startswith("whatsapp:") else from_wa
    info = normalize_payload(parsed)
    if from_wa.startswith("whatsapp:"):
        info["to_whatsapp"] = from_wa

    min_fields = ("servicio","cliente","m2","direccion","contacto")
    faltan = [k for k in min_fields if not info.get(k)]
    resp = MessagingResponse()

    if faltan:
        prompts = {
            "servicio":  "¿Qué *servicio* necesitas? (desinsectación / desratización / desinfección)",
            "cliente":   "¿Eres *Residencial* o *Comercial*?",
            "m2":        "¿Cuántos *m2* aproximados tiene el lugar?",
            "direccion": "¿Cuál es la *dirección* o ubicación del servicio?",
            "contacto":  "¿Nombre y *contacto* de la persona a cargo?",
        }
        msg = "Para generar tu cotización me faltan:\n"
        for f in faltan:
            msg += f"• {prompts.get(f, f'({f})')}\n"
        msg += "\nTambién puedes enviar todo en un solo mensaje así:\n" \
               "servicio: Desinsectación; m2: 120; direccion: Casa 123; comuna: Villarrica; contacto: Javiera; email: demo@correo.com; cliente: Residencial"
        resp.message(msg)
        return str(resp), 200, {"Content-Type": "application/xml"}

    ok, resumen, urls, err = generar_y_enviar_desde_info(info)
    if not ok:
        resp.message("⚠️ No pude generar la cotización.\nDetalle: " + (err or "error desconocido"))
        return str(resp), 200, {"Content-Type": "application/xml"}

    txt = "📄 Cotización generada.\n" \
          f"{resumen}\n\n" \
          f"🔗 PDF: {urls['pdf']}\n" \
          f"🔗 DOCX: {urls['docx']}"
    resp.message(txt)
    return str(resp), 200, {"Content-Type": "application/xml"}


# ========== MAIN ==========
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True, use_reloader=False)
