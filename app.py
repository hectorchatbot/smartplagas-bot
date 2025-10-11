# -*- coding: utf-8 -*-
import os, re, time, unicodedata, datetime, json, shutil, subprocess, logging, uuid
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from docxtpl import DocxTemplate
from werkzeug.utils import secure_filename
import redis

logging.basicConfig(level=logging.INFO)
load_dotenv(override=False)

# -----------------------------------------------------------------------------
# App + CORS
# -----------------------------------------------------------------------------
app = Flask(__name__)
ALLOWED_ORIGIN  = os.getenv("CORS_ALLOW_ORIGIN", "*")
ALLOWED_METHODS = "GET, POST, OPTIONS"
ALLOWED_HEADERS = "Content-Type, ngrok-skip-browser-warning, Authorization, X-Upload-Token"

@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"]  = ALLOWED_ORIGIN
    resp.headers["Access-Control-Allow-Methods"] = ALLOWED_METHODS
    resp.headers["Access-Control-Allow-Headers"] = ALLOWED_HEADERS
    return resp

# -----------------------------------------------------------------------------
# Redis (opcional: sesión y dedupe)
# -----------------------------------------------------------------------------
def _obtener_redis_url():
    for key in ("REDIS_URL", "UPSTASH_REDIS_URL", "REDIS_TLS_URL", "RAILWAY_REDIS_URL"):
        v = os.getenv(key)
        if v and v.strip():
            return v.strip()
    host = os.getenv("REDIS_HOST"); port = os.getenv("REDIS_PORT"); pwd = os.getenv("REDIS_PASSWORD")
    if host and port and pwd:
        esquema = "rediss" if os.getenv("REDIS_SSL", "true").lower() in ("1", "true", "yes") else "redis"
        return f"{esquema}://default:{pwd}@{host}:{port}"
    return None

REDIS_URL = _obtener_redis_url()
_r = None

def _conectar_redis():
    url = (REDIS_URL or "").strip()
    if not url:
        app.logger.info("REDIS_URL no definida. Continuando sin Redis.")
        return None
    try:
        cli = redis.from_url(url, decode_responses=True)
        cli.ping()
        app.logger.info("Conectado a Redis correctamente.")
        return cli
    except Exception as e:
        app.logger.warning(f"No se pudo conectar a Redis ({url}): {e}. Continuando sin Redis.")
        return None

_r = _conectar_redis()

def _sess_key(form: dict) -> str:
    waid = (form.get("WaId") or "").strip()
    if waid: return waid
    return (form.get("From") or "").replace("whatsapp:", "").strip()

def _sess_get(key: str):
    if not _r: return None
    v = _r.get(f"sess:{key}")
    return json.loads(v) if v else None

def _sess_set(key: str, val: dict, ttl_sec: int = 60*60*12):
    if not _r: return None
    _r.set(f"sess:{key}", json.dumps(val), ex=ttl_sec)

def _sess_exists(key: str) -> bool:
    if not _r: return False
    return _r.exists(f"sess:{key}") == 1

DEDUP_TTL = 300
def _dedup_should_process(msg_sid: str) -> bool:
    if not _r or not msg_sid: return True
    ok = _r.set(f"dedup:{msg_sid}", "1", nx=True, ex=DEDUP_TTL)
    return bool(ok)

# -----------------------------------------------------------------------------
# Entorno / Twilio
# -----------------------------------------------------------------------------
TW_SID   = os.getenv("TWILIO_ACCOUNT_SID", "")
TW_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TW_FROM  = os.getenv("TWILIO_WHATSAPP_FROM") or os.getenv("TWILIO_PHONE_NUMBER") or "whatsapp:+14155238886"
ADMIN_WA = (os.getenv("ADMIN_WA") or os.getenv("ADMIN_WHATSAPP") or os.getenv("MY_PHONE_NUMBER") or "whatsapp:+56995300790").strip()
TWILIO_ENABLED = (os.getenv("TWILIO_ENABLED", "true").lower() == "true")

BASE_URL = (os.getenv("BASE_URL") or os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
FILES_SUBDIR = (os.getenv("FILES_DIR", "out") or "out").strip()
FILES_DIR    = os.path.join(BASE_DIR, FILES_SUBDIR)
os.makedirs(FILES_DIR, exist_ok=True)

# Plantillas
TEMPLATE_PLAGAS   = os.path.join(BASE_DIR, "templates", "templatescotizacion_plagas.docx")
TEMPLATE_PISCINAS = os.path.join(BASE_DIR, "templates", "templatescotizacion_piscinas.docx")
TEMPLATE_CAMARAS  = os.path.join(BASE_DIR, "templates", "templatescotizacion_camaras.docx")

SEND_PDF    = (os.getenv("SEND_PDF_TO_CLIENT", "true").lower() == "true")
SEND_DOC    = (os.getenv("SEND_DOC_TO_CLIENT", "false").lower() == "true")
MEDIA_DELAY = float(os.getenv("MEDIA_DELAY_SECONDS", "1.0"))
SEND_COPY_TO_ADMIN = (os.getenv("SEND_COPY_TO_ADMIN", "true").lower() == "true")

twilio = Client(TW_SID, TW_TOKEN) if (TW_SID and TW_TOKEN) else None

# -----------------------------------------------------------------------------
# Precios y utilidades de negocio
# -----------------------------------------------------------------------------
TRAMOS = [(0,50),(51,100),(101,200),(201,300),(301,500),(501,1000),(1001,2000),(2001,9999999)]
PRECIOS = {
    "desinsectacion":[37500,47500,65000,80000,105000,165000,270000,440000],
    "desratizacion": [34000,44000,60000,75000, 97500,150000,235000,375000],
    "desinfeccion":  [30000,40000,55000,70000, 90000,140000,220000,350000],
}
TRAMOS_M3 = [(0,25),(26,50),(51,100),(101,999999)]
PRECIOS_PISCINA = {
    "piscina_plan_intermedio_m3":  [3900,3400,3100,2900],
    "piscina_mantencion_bomba_m3": [3200,3000,2800,2600],
    "piscina_shock_m3":            [1500,1300,1100,900],
    "piscina_diagnostico_total":   [30000,35000,40000,45000],
    "piscina_cambio_arena_total":  [90000,140000,200000,300000],
}
CAM_PRECIOS = {
    "alambricas":   {"interior":70000,"exterior":90000},
    "inalambricas": {"interior":60000,"exterior":80000},
    "solares":      {"exterior":150000},
    "dvr":          {"interior":75000,"exterior":95000},
}

def _fmt_money_clp(v:int)->str: return f"${v:,}".replace(",", ".")

def _descuento_por_cantidad(qty: int) -> float:
    if qty >= 5: return 0.85
    if qty >= 3: return 0.90
    if qty == 2: return 0.95
    return 1.00

def _infer_area_from_text(txt: str, tipo_camara: str) -> str:
    if (tipo_camara or "").lower().startswith("sola"): return "exterior"
    t = (txt or "").lower()
    exterior_words = ("exterior","patio","jardin","jardín","porton","portón","entrada","estacionamiento","perimetro","perímetro","terraza","muro")
    return "exterior" if any(w in t for w in exterior_words) else "interior"

def _canon_tipo_camara(s: str) -> str:
    s = (s or "").strip().lower()
    if "dvr" in s or "grabador" in s: return "dvr"
    if "inalam" in s or "wi fi" in s or "wi-fi" in s or "wifi" in s: return "inalambricas"
    if "sola" in s: return "solares"
    return "alambricas"

def _cantidad_aproximada(opcion: str) -> int:
    t = (opcion or "").lower()
    if "1" in t and "2" in t: return 2
    if "3" in t and "5" in t: return 4
    if "mas" in t or "más" in t or "5" in t: return 6
    m = re.search(r"\d+", t)
    return int(m.group(0)) if m else 1

def calcular_total_camaras(tipo_camara_humano: str, area_vigilar: str, cantidad_opcion: str):
    tipo = _canon_tipo_camara(tipo_camara_humano)
    qty  = _cantidad_aproximada(cantidad_opcion)
    area = _infer_area_from_text(area_vigilar, tipo)
    tabla = CAM_PRECIOS.get(tipo, {})
    if area not in tabla: area = next(iter(tabla.keys()), "exterior")
    base_unit = int(tabla[area])
    unit = int(round(base_unit * _descuento_por_cantidad(qty)))
    return unit * qty, tipo, qty, unit, area

def _strip_accents_and_symbols(text: str) -> str:
    t = text or ""
    t = re.sub(r"[\u2460-\u24FF\u2600-\u27BF\ufe0f\u200d]", "", t)
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    return re.sub(r"[^a-zA-Z0-9\s]", " ", t).lower().strip()

def _norm(s: str) -> str:
    if not s: return ""
    s = s.strip().lower()
    s = re.sub(r"[\u2460-\u24FF\u2600-\u27BF\ufe0f\u200d]", "", s)
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip()

def _dominio_servicio(label: str) -> str:
    s = _norm(label)
    if "piscin" in s: return "piscinas"
    if any(k in s for k in ("plaga","desratiz","desinsect","sanitiz")): return "plagas"
    if "camar" in s: return "camaras"
    return "otro"

def _canon_servicio_para_precios(servicio_humano: str) -> str:
    s = _strip_accents_and_symbols(servicio_humano)
    if "desratiz" in s:  return "desratizacion"
    if "desinfecc" in s: return "desinfeccion"
    if "desinsect" in s: return "desinsectacion"
    return "desinsectacion"

def _canon_piscina_key(label: str) -> str:
    s = _norm(label)
    if "plan intermedio" in s or ("tratamient" in s and "limpiez" in s): return "piscina_plan_intermedio_m3"
    if ("bomba" in s) or ("filtro" in s) or ("mantencion" in s):         return "piscina_mantencion_bomba_m3"
    if ("shock" in s) or ("clor" in s):                                   return "piscina_shock_m3"
    if ("diagn" in s):                                                    return "piscina_diagnostico_total"
    if ("arena" in s) or ("carga" in s):                                  return "piscina_cambio_arena_total"
    return ""

def precio_por_tramo(servicio_precio: str, m2: float) -> int:
    tabla = PRECIOS.get(servicio_precio)
    if not tabla: return 0
    m2n = int(float(m2) if m2 else 0)
    for idx, (lo, hi) in enumerate(TRAMOS):
        if lo <= m2n <= hi: return int(tabla[idx])
    return int(tabla[-1])

def _volumen_estimado_m3(info: dict) -> float:
    for k in ("m3","volumen","volumen_m3"):
        v = str(info.get(k, "") or "").strip()
        if v:
            try: return float(v.replace(",", "."))
            except Exception: pass
    try: m2 = float(info.get("m2") or 0)
    except Exception: m2 = 0.0
    try:
        prof = float(str(info.get("profundidad") or "").replace(",", ".")) if info.get("profundidad") else None
    except Exception:
        prof = None
    if m2 > 0 and prof is not None and prof > 0:
        return round(m2 * prof, 1)
    return 0.0

def _precio_piscina_por_tramo(serv_key: str, m3: float) -> int:
    if m3 <= 0 and serv_key.endswith("_m3"): return 0
    tabla = PRECIOS_PISCINA.get(serv_key)
    if not tabla: return 0
    idx = len(TRAMOS_M3) - 1
    for i, (lo, hi) in enumerate(TRAMOS_M3):
        if lo <= m3 <= hi: idx = i; break
    if serv_key.endswith("_m3"):
        unit = tabla[idx]
        if unit <= 0: return 0
        return int(round(unit * m3))
    return int(tabla[idx] or 0)

def precio_total(info: dict) -> int:
    dominio = _dominio_servicio(info.get("servicio_label",""))
    if dominio == "piscinas":
        key = _canon_piscina_key(info.get("servicio_label",""))
        m3 = _volumen_estimado_m3(info)
        return _precio_piscina_por_tramo(key, m3)
    if dominio == "plagas":
        return precio_por_tramo(info.get("servicio_precio",""), info.get("m2") or 0)
    if dominio == "camaras":
        total, _, _, _, _ = calcular_total_camaras(
            info.get("tipo_camara",""), info.get("area_vigilar",""), info.get("cantidad_camara","")
        )
        return total
    return 0

def _safe(x):
    if x is None: return ""
    if isinstance(x, (list, tuple)): return ", ".join(_safe(v) for v in x)
    if isinstance(x, dict):
        for k in ("label","title","name","value","text"):
            if k in x and x[k] not in (None,""): return _safe(x[k])
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

# -----------------------------------------------------------------------------
# DOCX -> PDF
# -----------------------------------------------------------------------------
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
    base_pdf = os.path.splitext(os.path.basename(docx_path))[0] + ".pdf"
    generated = os.path.join(outdir, base_pdf)
    if os.path.exists(generated) and generated != pdf_path:
        os.replace(generated, pdf_path)
    if not os.path.exists(pdf_path):
        raise RuntimeError("LibreOffice no generó el PDF")

def convertir_docx_a_pdf(docx_path: str, pdf_path: str) -> None:
    if os.name == "nt" and docx2pdf_convert is not None:
        time.sleep(0.2)
        com_init = False
        try:
            if pythoncom is not None:
                try: pythoncom.CoInitialize(); com_init = True
                except Exception: pass
            docx2pdf_convert(docx_path, pdf_path)
        finally:
            if com_init:
                try: pythoncom.CoUninitialize()
                except Exception: pass
        if os.path.exists(pdf_path): return
    convertir_docx_a_pdf_con_lo(docx_path, pdf_path)

# -----------------------------------------------------------------------------
# Render DOCX
# -----------------------------------------------------------------------------
def _select_template_path(info: dict) -> str:
    dom = _dominio_servicio(info.get("servicio_label",""))
    if dom == "plagas":   return TEMPLATE_PLAGAS
    if dom == "piscinas": return TEMPLATE_PISCINAS
    if dom == "camaras":  return TEMPLATE_CAMARAS
    return TEMPLATE_PLAGAS

def generar_docx_desde_plantilla(path: str, info: dict)->None:
    tpl_path = _select_template_path(info)
    if not os.path.exists(tpl_path):
        raise FileNotFoundError(f"Plantilla no encontrada: {tpl_path}")

    dom = _dominio_servicio(info.get("servicio_label",""))
    total_int = precio_total(info)

    ctx = {
        "fecha": info["fecha"],
        "cliente": info["cliente"],
        "direccion": info["direccion"],
        "comuna": info.get("comuna",""),
        "contacto": info["contacto"],
        "email": info["email"],
        "servicio": info["servicio_label"],
        "precio": _fmt_money_clp(total_int),
        "m2": "", "m3": "", "camaras": "",
        "cantidad_12": 0, "cantidad_34": 0, "cantidad_56": 0,
        "precio_12": "", "precio_34": "", "precio_56": "",
        "total": _fmt_money_clp(total_int),
    }

    if dom == "plagas":
        try:
            m2_val = float(info.get("m2", 0))
            ctx["m2"] = str(int(m2_val)) if m2_val.is_integer() else str(m2_val)
        except Exception:
            ctx["m2"] = str(info.get("m2", ""))
    elif dom == "piscinas":
        m3_val = _volumen_estimado_m3(info)
        ctx["m3"] = str(int(m3_val)) if m3_val and float(m3_val).is_integer() else str(m3_val or "")
    elif dom == "camaras":
        tot, tipo, qty, unit_ap, area = calcular_total_camaras(
            info.get("tipo_camara",""), info.get("area_vigilar",""), info.get("cantidad_camara","")
        )
        ctx["camaras"] = f"{info.get('tipo_camara','')} ({area}) x {qty} — {_fmt_money_clp(unit_ap)} c/u"
        c12 = qty if qty <= 2 else 0
        c34 = qty if 3 <= qty <= 4 else 0
        c56 = qty if qty >= 5 else 0
        ctx["cantidad_12"] = c12; ctx["cantidad_34"] = c34; ctx["cantidad_56"] = c56
        ctx["precio_12"] = _fmt_money_clp(unit_ap * c12) if c12 else ""
        ctx["precio_34"] = _fmt_money_clp(unit_ap * c34) if c34 else ""
        ctx["precio_56"] = _fmt_money_clp(unit_ap * c56) if c56 else ""
        ctx["total"] = _fmt_money_clp(tot); ctx["precio"] = _fmt_money_clp(tot)

    tpl = DocxTemplate(tpl_path)
    tpl.render(ctx)
    tpl.save(path)

# -----------------------------------------------------------------------------
# WhatsApp helpers
# -----------------------------------------------------------------------------
def send_whatsapp_text(to_wa: str, body: str, delay: float = 0.0):
    result = {}
    if not (twilio and TWILIO_ENABLED and to_wa and body):
        result["warn"] = "twilio_or_params_missing_or_disabled"; return result
    try:
        time.sleep(max(0.0, delay))
        msg = twilio.messages.create(from_=TW_FROM, to=to_wa, body=body)
        result["sid"] = msg.sid
    except Exception as e:
        result["error"] = str(e)
    return result

def send_whatsapp_media_only_pdf(to_wa: str, caption: str, pdf_url: str, delay: float = 0.0):
    result = {}
    if not (twilio and TWILIO_ENABLED and to_wa and pdf_url):
        result["warn"]="twilio_or_params_missing_or_disabled"; return result
    try:
        time.sleep(max(0.0, delay))
        msg = twilio.messages.create(from_=TW_FROM, to=to_wa, body=caption, media_url=[pdf_url])
        result["single_msg_sid"] = msg.sid
    except Exception as e:
        result["error"] = str(e)
    return result

def send_admin_copy(resumen_texto: str, pdf_url: str = "", docx_url: str = ""):
    if not (ADMIN_WA and TWILIO_ENABLED and twilio):
        return {"warn": "admin_or_twilio_not_configured"}
    sids = {}
    if resumen_texto:
        sids["admin_text"] = send_whatsapp_text(ADMIN_WA, "🧾 *Nueva cotización*\n\n" + resumen_texto, delay=0.0)
    if pdf_url:
        sids["admin_pdf"]  = send_whatsapp_media_only_pdf(ADMIN_WA, "📎 PDF de la cotización", pdf_url, delay=MEDIA_DELAY)
    if docx_url:
        sids["admin_docx"] = send_whatsapp_text(ADMIN_WA, f"📄 DOCX: {docx_url}", delay=MEDIA_DELAY)
    return sids

# -----------------------------------------------------------------------------
# Normalización de payload externo y generate
# -----------------------------------------------------------------------------
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
        "servicio_label": servicio_label, "servicio_precio": servicio_precio,
        "cliente": cliente, "m2": m2_num, "direccion": direccion, "comuna": comuna,
        "detalles": detalles, "contacto": contacto, "email": email, "to_whatsapp": to_wa
    }

def _read_payload_any():
    if request.is_json:
        data = request.get_json(silent=True)
        if isinstance(data, dict): return data
    try:
        raw = (request.data or b"").decode("utf-8").strip()
        if raw:
            j = json.loads(raw)
            if isinstance(j, dict): return j
    except Exception:
        pass
    if request.form: return {k:v for k,v in request.form.items()}
    return {}

def handle_generate():
    payload = _read_payload_any()
    info = normalize_payload(payload)
    faltantes = [k for k in ("servicio_label","cliente","direccion","contacto") if not info.get(k)]
    if faltantes:
        return jsonify(ok=True, message="Campos mínimos faltantes; no se generan archivos",
                       missing=faltantes, received=payload), 200

    if not any(os.path.exists(p) for p in (TEMPLATE_PLAGAS, TEMPLATE_PISCINAS, TEMPLATE_CAMARAS)):
        return jsonify(ok=False, error="template_missing", detail="No se encontraron plantillas DOCX en /templates"), 500

    if (docx2pdf_convert is None) and (not _lo_bin()):
        return jsonify(ok=False, error="pdf_engine_missing",
                       detail="No hay Word/docx2pdf ni LibreOffice disponibles para convertir a PDF."), 500

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    uid = uuid.uuid4().hex[:6]
    base = f"cotizacion_{ts}_{uid}"
    docx_name, pdf_name = base + ".docx", base + ".pdf"
    docx_path, pdf_path = os.path.join(FILES_DIR, docx_name), os.path.join(FILES_DIR, pdf_name)

    try:
        generar_docx_desde_plantilla(docx_path, info)
        convertir_docx_a_pdf(docx_path, pdf_path)
    except Exception as e:
        return jsonify(ok=False, error="doc_generate_failed", detail=str(e)), 500

    docx_url, pdf_url = build_urls(docx_name, pdf_name)
    total_int = precio_total(info)
    total = _fmt_money_clp(total_int)

    dominio = _dominio_servicio(info.get("servicio_label",""))
    medidas_line = ""; detalle_line = ""
    if dominio == "piscinas":
        try:
            vol = _volumen_estimado_m3(info)
            medidas_line = f"*Superficie:* {info.get('m2',0)} m²" + (f" | *Volumen:* {vol} m³" if vol > 0 else "") + "\n"
        except Exception:
            medidas_line = f"*Superficie:* {info.get('m2',0)} m²\n"
    elif dominio == "plagas":
        medidas_line = f"*Superficie tratada:* {info.get('m2',0)} m²\n"
    elif dominio == "camaras":
        tot, tipo, qty, unit_ap, area = calcular_total_camaras(
            info.get("tipo_camara",""), info.get("area_vigilar",""), info.get("cantidad_camara","")
        )
        detalle_line = f"*Cámaras:* {info.get('tipo_camara','')} ({area}) x {qty} — unit: {_fmt_money_clp(unit_ap)}\n"

    partes = [
        "✅ *Nueva solicitud recibida*\n",
        f"*Servicio:* {info['servicio_label']}\n",
        detalle_line,
        f"*Cliente:* {info['cliente']}\n",
        medidas_line,
        f"*Ubicación:* {info['direccion']}\n",
    ]
    if info.get("comuna"): partes.append(f"*Comuna:* {info['comuna']}\n")
    partes.extend([f"*Detalles:* {info.get('detalles','')}\n",
                   f"*Contacto:* {info['contacto']} | {info['email']}\n", f"*Total:* {total}"])
    resumen = "".join(partes)

    sids = {}
    if info.get("to_whatsapp") and SEND_PDF:
        sids["client_pdf"] = send_whatsapp_media_only_pdf(info["to_whatsapp"], "📎 Cotización adjunta", pdf_url, MEDIA_DELAY)
        if SEND_DOC:
            send_whatsapp_text(info["to_whatsapp"], f"📄 DOCX: {docx_url}", delay=MEDIA_DELAY)

    if SEND_COPY_TO_ADMIN and ADMIN_WA:
        sids["admin"] = send_admin_copy(resumen, pdf_url, docx_url)

    return jsonify(ok=True, resumen=resumen, docx_url=docx_url, pdf_url=pdf_url,
                   to_wa=info.get("to_whatsapp",""), twilio=sids), 200

# -----------------------------------------------------------------------------
# FLUJO (JSON)
# -----------------------------------------------------------------------------
FLOW_PATH = os.path.join(BASE_DIR, "chatbot-flujo.json")
FLOW_ENABLED = True
FLOW, FLOW_INDEX, FIRST_NODE_ID = [], {}, None

def _load_flow():
    global FLOW, FLOW_INDEX, FIRST_NODE_ID
    FLOW_INDEX.clear(); FLOW[:] = []
    if os.path.exists(FLOW_PATH):
        with open(FLOW_PATH, "r", encoding="utf-8") as f: FLOW[:] = json.load(f)
        for node in FLOW: FLOW_INDEX[str(node.get("id"))] = node
        FIRST_NODE_ID = str(FLOW[0]["id"]) if FLOW else None
_load_flow()

def _render_template_text(text:str, data:dict)->str:
    def repl(m): return str(data.get(m.group(1).strip(),""))
    return re.sub(r"\{([^}]+)\}", repl, text or "")

def _reply(resp: MessagingResponse, text:str):
    if text: resp.message(text)

def _present_options(node):
    opts = node.get("options",[]) or []
    lines=[]
    for i,opt in enumerate(opts,1):
        t=(opt.get("text","") or "").strip()
        if not re.match(r"^\d", t): t=f"{i}. {t}"
        lines.append(t)
    return "\n".join(lines)

def _clean_option_text(t:str)->str:
    t=t.strip(); t=re.sub(r"^[0-9\W_]+","",t).strip(); return t

def _rango_to_m2(r:str)->float:
    s = _strip_accents_and_symbols(r)
    if "menos" in s or "<" in s: return 80.0
    if "100" in s and "200" in s: return 150.0
    if "mas" in s or "más" in s or "200" in s: return 220.0
    m = re.search(r"(\d{2,4})", r)
    return float(m.group(1)) if m else 0.0

def _parse_piscina_to_m2(tamano:str)->float:
    if not tamano: return 0.0
    m=re.search(r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)", tamano.lower())
    if not m: return 0.0
    a=float(m.group(1).replace(",", ".")); b=float(m.group(2).replace(",", "."))
    return round(a*b,1)

def _session_info_to_generator_fields(data:dict, from_wa:str)->dict:
    base=(data.get("servicio") or "").strip()
    sub =(data.get("subservicio") or "").strip()
    label=f"{base} - {sub}" if sub else base
    m2=0.0
    if data.get("m2"):
        try: m2=float(str(data["m2"]).replace(",", "."))
        except Exception: m2=0.0
    if not m2 and data.get("rango_m2"):       m2 = _rango_to_m2(data["rango_m2"])
    if not m2 and data.get("tamano_piscina"): m2 = _parse_piscina_to_m2(data["tamano_piscina"])
    serv_precio=_canon_servicio_para_precios(label)
    info={
        "fecha": datetime.date.today().strftime("%d-%m-%Y"),
        "servicio_label": label or "Desinsectación",
        "servicio_precio": serv_precio,
        "cliente": "Residencial",
        "m2": m2 or 0,
        "direccion": data.get("direccion",""),
        "comuna":    data.get("comuna",""),
        "detalles":  data.get("area_vigilar",""),
        "contacto":  data.get("nombre",""),
        "email":     data.get("email",""),
        "to_whatsapp": from_wa if from_wa.startswith("whatsapp:") else "",
        "tamano_piscina": data.get("tamano_piscina",""),
        "profundidad":    data.get("profundidad",""),
        "tipo_camara":     data.get("tipo_camara",""),
        "cantidad_camara": data.get("cantidad_camara",""),
        "area_vigilar":    data.get("area_vigilar",""),
        "telefono":        data.get("telefono",""),
    }
    return info

def _send_estimate_and_files(resp, info, resumen_breve=""):
    if not any(os.path.exists(p) for p in (TEMPLATE_PLAGAS, TEMPLATE_PISCINAS, TEMPLATE_CAMARAS)):
        _reply(resp, "⚠️ No se encontraron plantillas de cotización."); return
    if (docx2pdf_convert is None) and (not _lo_bin()):
        _reply(resp, "⚠️ No hay motor de PDF disponible (Word/docx2pdf o LibreOffice)."); return

    ts=datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base=f"cotizacion_{ts}"
    docx_name, pdf_name = base+".docx", base+".pdf"
    docx_path, pdf_path = os.path.join(FILES_DIR, docx_name), os.path.join(FILES_DIR, pdf_name)
    try:
        generar_docx_desde_plantilla(docx_path, info)
        convertir_docx_a_pdf(docx_path, pdf_path)
    except Exception as e:
        _reply(resp, "⚠️ No pude generar tu documento: "+str(e)); return

    docx_url, pdf_url = build_urls(docx_name, pdf_name)
    total_int = precio_total(info); total_txt = _fmt_money_clp(total_int)

    dominio = _dominio_servicio(info.get("servicio_label",""))
    medidas_txt = ""; detalle_line = ""
    try:
        if dominio == "piscinas":
            prof = float(str(info.get("profundidad","") or "0").replace(",", ".")) if str(info.get("profundidad","")).strip() else 0.0
            if prof > 0 and float(info.get("m2",0)) > 0:
                vol_calc = round(float(info["m2"]) * prof, 1)
                medidas_txt = f"💧 *Volumen estimado:* {vol_calc} m³\n🧱 *Superficie:* {info.get('m2', 0)} m²\n"
            else:
                medidas_txt = f"🧱 *Superficie:* {info.get('m2', 0)} m²\n"
        elif dominio == "plagas":
            medidas_txt = f"🏠 *Superficie tratada:* {info.get('m2', 0)} m²\n"
        elif dominio == "camaras":
            tot, tipo, qty, unit_ap, area = calcular_total_camaras(
                info.get("tipo_camara",""), info.get("area_vigilar",""), info.get("cantidad_camara",""),
            )
            detalle_line = f"*Cámaras:* {info.get('tipo_camara','')} ({area}) x {qty}  — unit: {_fmt_money_clp(unit_ap)}\n"
    except Exception:
        pass

    detalle_p=f"\n🧮 Tamaño piscina: {info['tamano_piscina']}" if info.get("tamano_piscina") else ""
    msg=(f"📄 He preparado tu estimado.\n"
         f"*Servicio:* {info['servicio_label']}{detalle_p}\n"
         f"{detalle_line}{medidas_txt}"
         f"💵 *Estimado:* {total_txt} CLP\n"
         f"_Vigencia 7 días. Sujeto a visita técnica._\n\n"
         f"📎 *PDF:* {pdf_url}\n")
    if SEND_DOC: msg += f"📄 *DOCX:* {docx_url}\n\n"
    _reply(resp, msg)

    if SEND_PDF and info.get("to_whatsapp"):
        send_whatsapp_media_only_pdf(info["to_whatsapp"], "📎 Cotización adjunta", pdf_url, MEDIA_DELAY)
        if SEND_DOC: send_whatsapp_text(info["to_whatsapp"], f"📄 DOCX: {docx_url}", delay=MEDIA_DELAY)

    if dominio == "piscinas": medida_admin = f" | m²: {info.get('m2',0)}"
    elif dominio == "plagas":  medida_admin = f" | m² tratados: {info.get('m2',0)}"
    elif dominio == "camaras":
        tot, tipo, qty, unit_ap, area = calcular_total_camaras(
            info.get("tipo_camara",""), info.get("area_vigilar",""), info.get("cantidad_camara",""),
        )
        medida_admin = f" | cámaras: {info.get('tipo_camara','')} ({area}) x {qty} unit:{_fmt_money_clp(unit_ap)}"
    else: medida_admin = ""
    resumen_admin=(f"👤 Cliente: {info.get('contacto','')} | {info.get('email','')} | {info.get('telefono','')}\n"
                   f"🧰 Servicio: {info['servicio_label']}{medida_admin}\n"
                   f"📍 Ubicación: {info.get('direccion','')}, {info.get('comuna','')}\n"
                   f"💵 Total (estimado): {total_txt}")
    if SEND_COPY_TO_ADMIN and ADMIN_WA:
        send_admin_copy(resumen_admin, pdf_url, docx_url)

# -----------------------------------------------------------------------------
# Rutas básicas
# -----------------------------------------------------------------------------
@app.get("/")
@app.get("/redis-ping")
def redis_ping():
    if not _r: return jsonify(ok=False, error="redis_disabled_or_unconfigured"), 503
    try: return jsonify(ok=True, pong=_r.ping()), 200
    except Exception as e: return jsonify(ok=False, error=str(e)), 500

@app.get("/health")
def health(): return jsonify(ok=True, service="smartplagas-bot", time=datetime.datetime.utcnow().isoformat()+"Z")

@app.route("/files/<path:filename>")
def files(filename): return send_from_directory(FILES_DIR, filename, as_attachment=False)

# -----------------------------------------------------------------------------
# /generate (REST)
# -----------------------------------------------------------------------------
@app.post("/generate")
def generate(): return handle_generate()

# -----------------------------------------------------------------------------
# /upload único (acepta Authorization: Bearer y X-Upload-Token)
# -----------------------------------------------------------------------------
UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "").strip()

@app.route("/upload", methods=["POST", "OPTIONS"])
def upload_pdf():
    # Preflight CORS
    if request.method == "OPTIONS":
        return ("", 204)

    # Token por Authorization: Bearer ... o X-Upload-Token
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        token = request.headers.get("X-Upload-Token", "").strip()

    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return jsonify(ok=False, error="unauthorized"), 401

    # Acepta varias claves de archivo
    f = request.files.get("file") or request.files.get("pdf") or request.files.get("document")
    if not f or not f.filename:
        return jsonify(ok=False, error="missing file"), 400

    os.makedirs(FILES_DIR, exist_ok=True)
    safe_name = secure_filename(f.filename or "archivo.pdf")
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_name = f"{ts}_{safe_name}"
    out_path = os.path.join(FILES_DIR, out_name)
    f.save(out_path)

    public = public_base_from_request().rstrip("/")
    url = f"{public}/files/{out_name}"
    return jsonify(ok=True, url=url, saved=out_name), 200

# -----------------------------------------------------------------------------
# Webhook de Twilio (flujo)
# -----------------------------------------------------------------------------
FLOW, FLOW_INDEX  # ya cargados
CAMERA_NODE_IDS = {"1748913058876","1748913223390","1748913354726","1748913446918","1748913856796"}
M2_NODE_ID = "1748911555017"

def _fix_next_hop(sess: dict, current_node: dict, next_id: str) -> str:
    servicio_raw = (sess.get("data", {}).get("servicio") or "")
    servicio = _norm(servicio_raw)
    is_camera_service = ("camar" in servicio)
    logging.info(f"[next-hop] next_id={next_id} servicio='{servicio_raw}' (norm='{servicio}') is_camera={is_camera_service}")
    if next_id in CAMERA_NODE_IDS and not is_camera_service: return M2_NODE_ID
    return next_id

def _advance_flow_until_input(resp: MessagingResponse, sess: dict, skey: str = None):
    while True:
        node = FLOW_INDEX.get(str(sess["node_id"]))
        if not node:
            _reply(resp, "Advertencia: No pude continuar el flujo. Escribe *reiniciar*.")
            return "stop"

        ntype   = node.get("type"); content = node.get("content","")
        varname = (node.get("variableName") or "").strip()
        nextId  = str(node.get("nextId") or "")

        if ntype == "mensaje":
            _reply(resp, _render_template_text(content, sess["data"]))
            if not nextId:
                if skey: _sess_set(skey, sess); return "final"
            sess["node_id"] = nextId
            if skey: _sess_set(skey, sess)
            continue

        elif ntype == "pregunta":
            _reply(resp, _render_template_text(content, sess["data"]))
            sess["last_question"]   = varname if varname else None
            sess["pending_next_id"] = nextId if nextId else None
            sess.pop("awaiting_option_for", None)
            if skey: _sess_set(skey, sess)
            return "wait_input"

        elif ntype == "condicional":
            txt = _render_template_text(content, sess["data"])
            opts = _present_options(node)
            _reply(resp, f"{txt}\n{opts}" if opts else txt)
            sess["awaiting_option_for"] = node["id"]
            sess["last_question"] = None
            sess["pending_next_id"] = None
            if skey: _sess_set(skey, sess)
            return "wait_option"

        else:
            _reply(resp, "⚠️ Tipo de bloque no reconocido.")
            if skey: _sess_set(skey, sess)
            return "stop"

def _choose_option(node, body):
    if not node or node.get("type") != "condicional": return (None, None, None)
    opts = node.get("options", []) or []
    if not opts: return (None, None, None)
    try:
        num = int(re.sub(r"\D", "", body))
        if 1 <= num <= len(opts):
            opt = opts[num - 1]
            return (opt.get("saveAs") or None, (opt.get("text") or "").strip(), str(opt.get("nextId") or ""))
    except Exception:
        pass
    cleaned = _clean_option_text(body).lower()
    for opt in opts:
        opt_text = _clean_option_text(opt.get("text") or "").lower()
        if cleaned and cleaned in opt_text:
            return (opt.get("saveAs") or None, (opt.get("text") or "").strip(), str(opt.get("nextId") or ""))
    return (None, None, None)

@app.route("/webhook", methods=["GET", "POST", "HEAD"])
def webhook():
    if request.method != "POST":
        return "ok", 200, {"Content-Type": "text/plain"}
    try:
        data = request.form.to_dict() if not request.is_json else (request.get_json() or {})
        body = (data.get("Body") or "").strip()
        body_lc = body.lower()
        msg_sid = (data.get("MessageSid") or "").strip()

        if not _dedup_should_process(msg_sid):
            return str(MessagingResponse()), 200, {"Content-Type":"application/xml"}

        skey = _sess_key(data)
        from_wa = data.get("From","").strip()
        resp = MessagingResponse()

        if body_lc in {"hola","buenas","hey","buenos dias","buenas tardes","buenas noches"}:
            sess = {"node_id": FIRST_NODE_ID, "data": {}, "last_question": None, "pending_next_id": None,
                    "awaiting_option_for": None, "last_msg_sid": msg_sid}
            _sess_set(skey, sess); _advance_flow_until_input(resp, sess, skey)
            return str(resp), 200, {"Content-Type":"application/xml"}

        if body_lc == "reiniciar":
            sess = {"node_id": FIRST_NODE_ID, "data": {}, "last_question": None, "pending_next_id": None,
                    "awaiting_option_for": None, "last_msg_sid": msg_sid}
            _sess_set(skey, sess); _reply(resp, "🔄 Flujo reiniciado. Iniciando atención…")
            _advance_flow_until_input(resp, sess, skey)
            return str(resp), 200, {"Content-Type":"application/xml"}

        if not _sess_exists(skey):
            sess = {"node_id": FIRST_NODE_ID, "data": {}, "last_question": None, "pending_next_id": None,
                    "awaiting_option_for": None, "last_msg_sid": None}
            _sess_set(skey, sess); _advance_flow_until_input(resp, sess, skey)
            return str(resp), 200, {"Content-Type":"application/xml"}

        sess = _sess_get(skey)
        if msg_sid and sess.get("last_msg_sid") == msg_sid:
            return str(MessagingResponse()), 200, {"Content-Type":"application/xml"}

        if sess.get("awaiting_option_for"):
            node_id = str(sess["awaiting_option_for"]); node = FLOW_INDEX.get(node_id)
            if not node:
                _reply(resp, "⚠️ Ha ocurrido un error. Escribe *reiniciar* para comenzar de nuevo.")
                return str(resp), 200, {"Content-Type":"application/xml"}

            saveAs, value, nextId = _choose_option(node, body)
            if not nextId:
                txt = _render_template_text(node.get("content",""), sess["data"])
                opts = _present_options(node)
                _reply(resp, f"❓ No entendí tu selección. Responde con el *número*.\n\n{txt}\n{opts}")
                return str(resp), 200, {"Content-Type":"application/xml"}

            if saveAs:
                if saveAs == "servicio":
                    val_norm = _norm(value)
                    body_num = re.sub(r"\D", "", body).strip()
                    canon = "otro"
                    if "plaga" in val_norm or body_num == "1": canon = "plagas"
                    elif "piscin" in val_norm or body_num == "2": canon = "piscinas"
                    elif "camar" in val_norm or body_num == "3": canon = "camaras"
                    sess["data"]["servicio"] = canon
                else:
                    sess["data"][saveAs] = value

            nextId = _fix_next_hop(sess, node, nextId)
            if saveAs == "subservicio": sess["data"]["subservicio"] = value

            sess["node_id"] = nextId; sess["awaiting_option_for"] = None; sess["last_msg_sid"] = msg_sid
            _sess_set(skey, sess); _advance_flow_until_input(resp, sess, skey)
            return str(resp), 200, {"Content-Type":"application/xml"}

        if sess.get("last_question"):
            var = sess["last_question"]
            sess["data"][var] = body
            nextId = sess.get("pending_next_id")
            sess["last_question"] = None; sess["pending_next_id"] = None; sess["last_msg_sid"] = msg_sid

            if var == "telefono":
                info = _session_info_to_generator_fields(sess["data"], from_wa)
                _send_estimate_and_files(resp, info)

            if nextId: sess["node_id"] = str(nextId)
            _sess_set(skey, sess)
            if nextId: _advance_flow_until_input(resp, sess, skey)
            else: _reply(resp, "Gracias. Escribe *reiniciar* si deseas empezar otra solicitud.")
            return str(resp), 200, {"Content-Type":"application/xml"}

        _reply(resp, "🤖 No entendí tu mensaje. Escribe *reiniciar* para comenzar nuevamente.")
        return str(resp), 200, {"Content-Type":"application/xml"}

    except Exception:
        logging.exception("❌ Error en webhook")
        resp = MessagingResponse()
        resp.message("Lo siento, ocurrió un error inesperado. Escribe *reiniciar* para empezar de nuevo.")
        return str(resp), 200, {"Content-Type": "application/xml"}

# -----------------------------------------------------------------------------
# Reload de flujo
# -----------------------------------------------------------------------------
@app.post("/reload-flow")
def reload_flow():
    try:
        _load_flow(); return jsonify(ok=True, count=len(FLOW)), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

def _log_url_map():
    try: logging.info("URL MAP:\n%s", app.url_map)
    except Exception: pass

_log_url_map()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True, use_reloader=False)
