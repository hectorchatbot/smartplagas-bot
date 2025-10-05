# -*- coding: utf-8 -*-
import os, re, time, unicodedata, datetime, json, shutil, subprocess, logging
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory, abort
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from docxtpl import DocxTemplate

# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------
# Carga .env
# ---------------------------------------------------------------------
load_dotenv(override=True)

# ---------------------------------------------------------------------
# Flask + CORS
# ---------------------------------------------------------------------
app = Flask(__name__)
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

# ---------------------------------------------------------------------
# Variables de entorno
# ---------------------------------------------------------------------
TW_SID   = os.getenv("TWILIO_ACCOUNT_SID", "")
TW_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TW_FROM  = os.getenv("TWILIO_WHATSAPP_FROM") or os.getenv("TWILIO_PHONE_NUMBER") or "whatsapp:+14155238886"
ADMIN_WA = os.getenv("ADMIN_WHATSAPP") or os.getenv("MY_PHONE_NUMBER") or ""

BASE_URL = (os.getenv("BASE_URL") or os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
FILES_SUBDIR = (os.getenv("FILES_DIR", "out") or "out").strip()
FILES_DIR    = os.path.join(BASE_DIR, FILES_SUBDIR)
os.makedirs(FILES_DIR, exist_ok=True)

TEMPLATE_DOCX = os.getenv("TEMPLATE_DOCX", os.path.join(BASE_DIR, "templates", "templatescotizacion_template.docx"))

SEND_PDF    = (os.getenv("SEND_PDF_TO_CLIENT", "true").lower() == "true")
SEND_DOC    = (os.getenv("SEND_DOC_TO_CLIENT", "false").lower() == "true")
MEDIA_DELAY = float(os.getenv("MEDIA_DELAY_SECONDS", "1.0"))

twilio = Client(TW_SID, TW_TOKEN) if (TW_SID and TW_TOKEN) else None

# ---------------------------------------------------------------------
# Precios
# ---------------------------------------------------------------------
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

# ---------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------
def _strip_accents_and_symbols(text: str) -> str:
    t = text or ""
    # quita emojis/números circ.
    t = re.sub(r"[\u2460-\u24FF\u2600-\u27BF\ufe0f\u200d]", "", t)
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    return re.sub(r"[^a-zA-Z0-9\s]", " ", t).lower().strip()

def _canon_servicio_para_precios(servicio_humano: str) -> str:
    s = _strip_accents_and_symbols(servicio_humano)
    if "desratiz" in s:   return "desratizacion"
    if "desinfecc" in s:  return "desinfeccion"
    if "desinsect" in s:  return "desinsectacion"
    return "desinsectacion"

def precio_por_tramo(servicio_precio: str, m2: float) -> int:
    tabla = PRECIOS.get(servicio_precio)
    if not tabla: return 0
    m2n = int(float(m2) if m2 else 0)
    for idx, (lo, hi) in enumerate(TRAMOS):
        if lo <= m2n <= hi:
            return int(tabla[idx])
    return int(tabla[-1])

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

# ---------------------------------------------------------------------
# Conversión DOCX -> PDF
# ---------------------------------------------------------------------
try:
    from docx2pdf import convert as docx2pdf_convert
except Exception:
    docx2pdf_convert = None
try:
    import pythoncom
except Exception:
    pythoncom = None

def _lo_bin():
    for name in ("soffice", "libreoffice"):
        if shutil.which(name):
            return name
    return None

def convertir_docx_a_pdf_con_lo(docx_path: str, pdf_path: str) -> None:
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
    convertir_docx_a_pdf_con_lo(docx_path, pdf_path)

def generar_docx_desde_plantilla(path: str, info: dict) -> None:
    if not os.path.exists(TEMPLATE_DOCX):
        raise FileNotFoundError(f"Plantilla no encontrada: {TEMPLATE_DOCX}")
    tpl = DocxTemplate(TEMPLATE_DOCX)
    try:
        m2_txt = str(int(info["m2"])) if float(info["m2"]).is_integer() else str(info["m2"])
    except Exception:
        m2_txt = str(info["m2"])
    total = precio_por_tramo(info["servicio_precio"], info["m2"])
    context = {
        "fecha":     info["fecha"],
        "cliente":   info["cliente"],
        "direccion": info["direccion"],
        "comuna":    info.get("comuna",""),
        "contacto":  info["contacto"],
        "email":     info["email"],
        "servicio":  info["servicio_label"],
        "m2":        m2_txt,
        "precio":    _fmt_money_clp(total),
    }
    tpl.render(context)
    tpl.save(path)

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

# ---------------------------------------------------------------------
# Normalización para integraciones externas (/gen-cotizacion)
# ---------------------------------------------------------------------
def normalize_payload(data: dict) -> dict:
    data = data or {}
    servicio  = _safe(data.get("servicioinicial") or data.get("servicio") or data.get("servicio_inicial"))
    cliente   = _safe(data.get("tipo_clientes")   or data.get("cliente")  or data.get("tipo_cliente") or "Residencial")
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

    servicio_label  = servicio or "Desinsectación"
    servicio_precio = _canon_servicio_para_precios(servicio_label)

    return {
        "fecha": datetime.date.today().strftime("%d-%m-%Y"),
        "servicio_label": servicio_label,
        "servicio_precio": servicio_precio,
        "cliente": cliente,
        "m2": m2_num,
        "direccion": direccion,
        "comuna": comuna,
        "detalles": detalles,
        "contacto": contacto,
        "email": email,
        "to_whatsapp": to_wa
    }

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

# ---------------------------------------------------------------------
# Generación de cotización (usada por /gen-cotizacion y por flujo)
# ---------------------------------------------------------------------
def handle_generate():
    payload = _read_payload_any()
    info = normalize_payload(payload)

    faltantes = [k for k in ("servicio_label","cliente","m2","direccion","contacto") if not info.get(k)]
    if faltantes:
        return jsonify(ok=True, message="Campos mínimos faltantes; no se generan archivos",
                       missing=faltantes, received=payload), 200

    if not os.path.exists(TEMPLATE_DOCX):
        return jsonify(ok=False, error="template_missing",
                       detail=f"No se encontró la plantilla: {TEMPLATE_DOCX}"), 500

    if (docx2pdf_convert is None) and (not _lo_bin()):
        return jsonify(ok=False, error="pdf_engine_missing",
                       detail="No hay Word/docx2pdf ni LibreOffice disponibles para convertir a PDF."), 500

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"cotizacion_{ts}"
    docx_name = base + ".docx"
    pdf_name  = base + ".pdf"
    docx_path = os.path.join(FILES_DIR, docx_name)
    pdf_path  = os.path.join(FILES_DIR, pdf_name)

    try:
        generar_docx_desde_plantilla(docx_path, info)
        convertir_docx_a_pdf(docx_path, pdf_path)
    except Exception as e:
        return jsonify(ok=False, error="doc_generate_failed", detail=str(e)), 500

    docx_url, pdf_url = build_urls(docx_name, pdf_name)

    total = _fmt_money_clp(precio_por_tramo(info["servicio_precio"], info["m2"]))
    resumen = (
        "✅ *Nueva solicitud recibida*\n"
        f"*Servicio:* {info['servicio_label']}\n"
        f"*Cliente:* {info['cliente']}\n"
        f"*Metros²:* {info['m2']}\n"
        f"*Ubicación:* {info['direccion']}\n"
        f"*Comuna:* {info.get('comuna','')}\n"
        f"*Detalles:* {info.get('detalles','')}\n"
        f"*Contacto:* {info['contacto']} | {info['email']}\n"
        f"*Total:* {total}"
    )

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

# ---------------------------------------------------------------------
# Rutas públicas (CORS)
# ---------------------------------------------------------------------
@app.route("/gen-cotizacion", methods=["POST", "OPTIONS"])
def gen_cotizacion():
    if request.method == "OPTIONS":
        return _cors_preflight()
    return handle_generate()

# ---------------------------------------------------------------------
# Archivos estáticos públicos
# ---------------------------------------------------------------------
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

# ---------------------------------------------------------------------
# Health / Debug
# ---------------------------------------------------------------------
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

@app.get("/debug")
def debug():
    return f"DEBUG OK - BASE_URL={BASE_URL}", 200

# ---------------------------------------------------------------------
# ======== Motor de flujo con chatbot-flujo.json (WhatsApp) ===========
# ---------------------------------------------------------------------
FLOW_PATH = os.path.join(BASE_DIR, "chatbot-flujo.json")
FLOW_ENABLED = True
FLOW = []
FLOW_INDEX = {}
FIRST_NODE_ID = None

def _load_flow():
    global FLOW, FLOW_INDEX, FIRST_NODE_ID
    FLOW_INDEX.clear()
    FLOW[:] = []
    if os.path.exists(FLOW_PATH):
        with open(FLOW_PATH, "r", encoding="utf-8") as f:
            FLOW[:] = json.load(f)
        for node in FLOW:
            FLOW_INDEX[str(node.get("id"))] = node
        FIRST_NODE_ID = str(FLOW[0]["id"]) if FLOW else None
    logging.info("Flujo cargado: %s nodos. first=%s", len(FLOW), FIRST_NODE_ID)

_load_flow()

@app.post("/reload-flow")
def reload_flow():
    _load_flow()
    return jsonify(ok=True, count=len(FLOW), first=FIRST_NODE_ID)

# Sesiones en memoria: { from_wa : {"node_id":..., "data": {...}, ... } }
SESSIONS = {}

def _render_template_text(text: str, data: dict) -> str:
    def repl(m):
        k = m.group(1).strip()
        return str(data.get(k, ""))
    return re.sub(r"\{([^}]+)\}", repl, text or "")

def _reply(resp: MessagingResponse, text: str):
    if text:
        resp.message(text)

def _present_options(node):
    lines = []
    for opt in node.get("options", []):
        lines.append(opt.get("text", "").strip())
    return "\n".join(lines)

def _clean_option_text(t: str) -> str:
    t = t or ""
    # quita prefijos como "1️⃣", "2)", "1.", etc
    t = re.sub(r"^\s*([0-9]+)[\s\.\)\-:_]*", "", t)
    t = re.sub(r"[\u2460-\u24FF\ufe0f\u200d]", "", t)  # emojis numerales
    return t.strip().lower()

def _digits_from_text(t: str) -> str:
    # extrae "1" de "1️⃣", "1.", "1)"...
    d = re.sub(r"\D", "", t or "")
    return d[:1] if d else ""

def _rango_to_m2(r: str) -> float:
    s = _strip_accents_and_symbols(r)
    if "menos" in s or "<" in s:  return 80.0
    if "100" in s and "200" in s: return 150.0
    if "mas" in s or ">" in s or "200" in s: return 220.0
    m = re.search(r"(\d{2,4})", r)
    return float(m.group(1)) if m else 0.0

def _parse_piscina_to_m2(tamano: str) -> float:
    if not tamano: return 0.0
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)", tamano.lower())
    if not m: return 0.0
    a = float(m.group(1).replace(",", "."))
    b = float(m.group(2).replace(",", "."))
    return round(a * b, 1)

def _session_info_to_generator_fields(data: dict, from_wa: str) -> dict:
    base = (data.get("servicio") or "").strip()
    sub  = (data.get("subservicio") or "").strip()
    label = f"{base} - {sub}" if sub else base

    m2 = 0.0
    if data.get("m2"):
        try: m2 = float(str(data["m2"]).replace(",", "."))
        except Exception: m2 = 0.0
    if not m2 and data.get("rango_m2"):
        m2 = _rango_to_m2(data["rango_m2"])
    if not m2 and data.get("tamano_piscina"):
        m2 = _parse_piscina_to_m2(data["tamano_piscina"])

    servicio_precio = _canon_servicio_para_precios(label)
    info = {
        "fecha": datetime.date.today().strftime("%d-%m-%Y"),
        "servicio_label": label or "Desinsectación",
        "servicio_precio": servicio_precio,
        "cliente": "Residencial",
        "m2": m2 or 0,
        "direccion": data.get("direccion",""),
        "comuna": data.get("comuna",""),
        "detalles": data.get("area_vigilar",""),
        "contacto": data.get("nombre",""),
        "email": data.get("email",""),
        "to_whatsapp": from_wa if (from_wa or "").startswith("whatsapp:") else ""
    }
    info["tamano_piscina"] = data.get("tamano_piscina","")
    info["telefono"] = data.get("telefono","")
    return info

def _send_estimate_and_files(resp, info, resumen_breve=""):
    if not os.path.exists(TEMPLATE_DOCX):
        _reply(resp, "⚠️ No se encontró la plantilla de cotización del sistema.")
        return
    if (docx2pdf_convert is None) and (not _lo_bin()):
        _reply(resp, "⚠️ No hay motor de PDF disponible (Word/docx2pdf o LibreOffice).")
        return

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"cotizacion_{ts}"
    docx_name = base + ".docx"
    pdf_name  = base + ".pdf"
    docx_path = os.path.join(FILES_DIR, docx_name)
    pdf_path  = os.path.join(FILES_DIR, pdf_name)

    try:
        generar_docx_desde_plantilla(docx_path, info)
        convertir_docx_a_pdf(docx_path, pdf_path)
    except Exception as e:
        _reply(resp, "⚠️ No pude generar tu documento: " + str(e))
        return

    docx_url, pdf_url = build_urls(docx_name, pdf_name)
    total_int = precio_por_tramo(info["servicio_precio"], info["m2"])
    total_txt = _fmt_money_clp(total_int)
    detalle_piscina = f"\n🧮 Tamaño piscina: {info['tamano_piscina']}" if info.get("tamano_piscina") else ""

    msg = (
        f"{resumen_breve}\n"
        f"*Servicio:* {info['servicio_label']}{detalle_piscina}\n"
        f"💵 *Estimado:* {total_txt} CLP (m2 aprox: {info['m2']})\n"
        f"_Vigencia 7 días. Sujeto a visita técnica._\n\n"
        f"📎 *PDF:* {pdf_url}\n"
        f"📄 *DOCX:* {docx_url}\n\n"
        f"¿Te agendo una visita esta semana? Responde *SI* o *NO*."
    )
    _reply(resp, msg)

    if SEND_PDF and info.get("to_whatsapp"):
        send_whatsapp_media_only_pdf(info["to_whatsapp"], "📎 Cotización adjunta", pdf_url, MEDIA_DELAY)
    if SEND_PDF and ADMIN_WA:
        resumen_admin = (
            "📣 Copia interna\n"
            f"Cliente: {info.get('contacto','')} | {info.get('email','')} | {info.get('telefono','')}\n"
            f"Servicio: {info['servicio_label']} | m2: {info['m2']}\n"
            f"Ubicación: {info.get('direccion','')}, {info.get('comuna','')}\n"
            f"Total: {total_txt}"
        )
        send_whatsapp_media_only_pdf(ADMIN_WA, resumen_admin, pdf_url, MEDIA_DELAY)

def _advance_flow_until_input(resp: MessagingResponse, sess: dict):
    """Avanza automáticamente por bloques de 'mensaje' hasta requerir input."""
    while True:
        node = FLOW_INDEX.get(str(sess["node_id"]))
        if not node:
            _reply(resp, "⚠️ No pude continuar el flujo. Escribe *reiniciar*.")
            return "stop"

        ntype   = node.get("type")
        content = node.get("content", "")
        varname = (node.get("variableName") or "").strip()
        nextId  = str(node.get("nextId") or "")

        if ntype == "mensaje":
            rendered = _render_template_text(content, sess["data"])
            _reply(resp, rendered)
            if nextId:
                sess["node_id"] = nextId
                continue
            return "final"

        elif ntype == "pregunta":
            _reply(resp, _render_template_text(content, sess["data"]))
            sess["last_question"]   = varname if varname else None
            sess["pending_next_id"] = nextId if nextId else None
            return "wait_input"

        elif ntype == "condicional":
            txt  = _render_template_text(content, sess["data"])
            opts = _present_options(node)
            _reply(resp, f"{txt}\n{opts}" if opts else txt)
            sess["awaiting_option_for"] = str(node["id"])
            return "wait_option"

        else:
            _reply(resp, "⚠️ Tipo de bloque no reconocido.")
            return "stop"

def _match_option(node, user_text: str):
    """Devuelve (idx, option) o (None, None). Acepta 1/2/3/4, emojis y coincidencia por texto."""
    if not node: return (None, None)
    options = node.get("options", []) or []
    if not options: return (None, None)

    # 1) por número (incluye emojis)
    d = _digits_from_text(user_text)
    if d:
        try:
            i = int(d) - 1
            if 0 <= i < len(options):
                return (i, options[i])
        except Exception:
            pass

    # 2) por texto limpio
    ut = _clean_option_text(user_text)
    for i, opt in enumerate(options):
        if ut and ut in _clean_option_text(opt.get("text","")):
            return (i, opt)

    return (None, None)

# ---------------------------------------------------------------------
# Webhook Twilio - recibe mensajes de WhatsApp
# ---------------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        # Twilio manda application/x-www-form-urlencoded
        data = request.form.to_dict() if not request.is_json else (request.get_json() or {})
        logging.info("Incoming: %s", data)

        body_raw = data.get("Body", "") or data.get("body", "")
        body = (body_raw or "").strip()
        body_norm = _strip_accents_and_symbols(body)
        from_wa = data.get("From", "") or data.get("from", "")
        to_wa   = data.get("To", "")   or data.get("to", "")

        resp = MessagingResponse()

        # Filtro de saludos: no lanza flujo
        if body_norm in ("hola", "buenas", "buenos dias", "buenas tardes", "hey"):
            _reply(resp, "👋 Hola! Bienvenido a *Smart Plagas*. Escribe *reiniciar* para comenzar tu atención.")
            return str(resp), 200, {"Content-Type": "application/xml"}

        # Reiniciar flujo
        if body_norm == "reiniciar":
            if not FIRST_NODE_ID:
                _reply(resp, "⚠️ Flujo no disponible. Intenta más tarde.")
                return str(resp), 200, {"Content-Type": "application/xml"}
            SESSIONS[from_wa] = {
                "node_id": FIRST_NODE_ID,
                "data": {},
                "last_question": None,
                "pending_next_id": None,
                "awaiting_option_for": None
            }
            _reply(resp, "🔄 Flujo reiniciado correctamente. Iniciando atención...")
            _advance_flow_until_input(resp, SESSIONS[from_wa])
            return str(resp), 200, {"Content-Type": "application/xml"}

        # Si no hay sesión activa
        sess = SESSIONS.get(from_wa)
        if not sess:
            _reply(resp, "Escribe *reiniciar* para comenzar tu atención en Smart Plagas.")
            return str(resp), 200, {"Content-Type": "application/xml"}

        # Si espera respuesta de condicional (opciones)
        if sess.get("awaiting_option_for"):
            node = FLOW_INDEX.get(sess["awaiting_option_for"])
            idx, opt = _match_option(node, body)
            if opt is None:
                _reply(resp, "⚠️ No entendí tu selección. Por favor escribe el *número* de la opción.")
                # re-mostrar opciones
                txt  = _render_template_text(node.get("content",""), sess["data"])
                opts = _present_options(node)
                _reply(resp, f"{txt}\n{opts}")
                return str(resp), 200, {"Content-Type": "application/xml"}

            # guardar si hay saveAs
            save_as = (opt.get("saveAs") or "").strip()
            if save_as:
                # valor limpio (sin emojis/num)
                value_clean = _clean_option_text(opt.get("text",""))
                sess["data"][save_as] = value_clean

            # pasar al siguiente bloque
            sess["node_id"] = str(opt.get("nextId") or "")
            sess["awaiting_option_for"] = None

            # Si justo después del condicional corresponde terminar o seguir preguntando
            status = _advance_flow_until_input(resp, sess)

            # Si llegamos al bloque final de agradecimiento y ya tenemos datos suficientes, generar cotización
            # Regla: cuando hay dirección + nombre + servicio, intentamos generar estimado
            try:
                if sess["data"].get("direccion") and (sess["data"].get("servicio") or sess["data"].get("subservicio")):
                    info = _session_info_to_generator_fields(sess["data"], from_wa)
                    _send_estimate_and_files(resp, info, "📄 He preparado tu estimado.")
            except Exception as e:
                logging.exception("send_estimate failed: %s", e)

            return str(resp), 200, {"Content-Type": "application/xml"}

        # Si espera respuesta de pregunta abierta
        if sess.get("last_question"):
            var = sess["last_question"]
            sess["data"][var] = body.strip()
            # mover al siguiente
            next_id = sess.get("pending_next_id")
            sess["last_question"] = None
            sess["pending_next_id"] = None
            if next_id:
                sess["node_id"] = str(next_id)
            status = _advance_flow_until_input(resp, sess)

            # Si tras contestar ya tenemos dirección + servicio, enviar estimado
            try:
                if sess["data"].get("direccion") and (sess["data"].get("servicio") or sess["data"].get("subservicio")):
                    info = _session_info_to_generator_fields(sess["data"], from_wa)
                    _send_estimate_and_files(resp, info, "📄 He preparado tu estimado.")
            except Exception as e:
                logging.exception("send_estimate failed: %s", e)

            return str(resp), 200, {"Content-Type": "application/xml"}

        # Si no esperaba nada específico
        _reply(resp, "🤖 No entendí tu mensaje. Escribe *reiniciar* para comenzar nuevamente.")
        return str(resp), 200, {"Content-Type": "application/xml"}

    except Exception as e:
        logging.exception("webhook error: %s", e)
        return str(MessagingResponse().message("⚠️ Error interno del servidor.")), 200, {"Content-Type": "application/xml"}

# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True, use_reloader=False)
