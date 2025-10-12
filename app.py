# -*- coding: utf-8 -*-
import os, re, time, unicodedata, datetime, json, shutil, subprocess, logging, uuid
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from docxtpl import DocxTemplate
from werkzeug.utils import secure_filename
import redis

# -----------------------------------------------------------------------------
# Config básica / logging
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
load_dotenv(override=False)
APP_VERSION = os.getenv("RAILWAY_GIT_COMMIT_SHA", "dev-local")

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
# Redis (opcional)
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

# Plantillas (sin bucles Jinja)
TEMPLATE_PLAGAS   = os.path.join(BASE_DIR, "templates", "templatescotizacion_plagas.docx")
TEMPLATE_PISCINAS = os.path.join(BASE_DIR, "templates", "templatescotizacion_piscinas.docx")
TEMPLATE_CAMARAS  = os.path.join(BASE_DIR, "templates", "templatescotizacion_camaras.docx")

SEND_PDF    = (os.getenv("SEND_PDF_TO_CLIENT", "true").lower() == "true")
SEND_DOC    = (os.getenv("SEND_DOC_TO_CLIENT", "false").lower() == "true")
MEDIA_DELAY = float(os.getenv("MEDIA_DELAY_SECONDS", "1.0"))
SEND_COPY_TO_ADMIN = (os.getenv("SEND_COPY_TO_ADMIN", "true").lower() == "true")

twilio = Client(TW_SID, TW_TOKEN) if (TW_SID and TW_TOKEN) else None

# -----------------------------------------------------------------------------
# Precios y utilidades
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

def _fmt_money_clp(v:int)->str:
    try:
        return f"${int(v):,}".replace(",", ".")
    except Exception:
        return "$0"

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
        label = info.get("servicio_label", "")
        override = (info.get("servicio_precio") or "").strip()
        if override in PRECIOS_PISCINA:
            key = override
        else:
            key = _canon_piscina_key(label) or "piscina_plan_intermedio_m3"
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
# Selección de plantilla + Render DOCX (SIN BUCLES)
# -----------------------------------------------------------------------------
def _select_template_path(info: dict) -> str:
    """
    Busca primero en /templates y si no hay nada, también en /out (FILES_DIR),
    por cualquier .docx que parezca plantilla.
    """
    dom = _dominio_servicio(info.get("servicio_label","")) or "otro"

    prefer_por_dom = {
        "plagas":   ["templatescotizacion_plagas.docx"],
        "piscinas": ["templatescotizacion_piscinas.docx"],
        "camaras":  ["templatescotizacion_camaras.docx"],
    }
    prefer = prefer_por_dom.get(dom, []) + [
        "templatescotizacion_template.docx",
        "templatescotizacion_plagas.docx",
        "templatescotizacion_piscinas.docx",
        "templatescotizacion_camaras.docx",
    ]

    dirs_a_buscar = [
        os.path.join(BASE_DIR, "templates"),
        FILES_DIR,  # también mira en /out por si subiste por /upload
    ]

    for d in dirs_a_buscar:
        for name in prefer:
            p = os.path.join(d, name)
            if os.path.exists(p):
                app.logger.info(f"[TPL] Usando plantilla preferida: {p}")
                return p

    for d in dirs_a_buscar:
        if os.path.isdir(d):
            for fname in os.listdir(d):
                if fname.lower().endswith(".docx") and "template" in fname.lower():
                    p = os.path.join(d, fname)
                    app.logger.info(f"[TPL] Usando plantilla genérica: {p}")
                    return p

    for d in dirs_a_buscar:
        if os.path.isdir(d):
            for fname in os.listdir(d):
                if fname.lower().endswith(".docx"):
                    p = os.path.join(d, fname)
                    app.logger.info(f"[TPL] Usando cualquier DOCX encontrado: {p}")
                    return p

    raise FileNotFoundError("No se encontraron plantillas DOCX en /templates ni en /out")


def generar_docx_desde_plantilla(path: str, info: dict) -> str:
    tpl_path = _select_template_path(info)
    if not os.path.exists(tpl_path):
        raise FileNotFoundError(f"Plantilla no encontrada: {tpl_path}")

    dom = _dominio_servicio(info.get("servicio_label", ""))
    total_int = precio_total(info)

    ctx = {
        "fecha": info["fecha"],
        "cliente": info["cliente"],
        "direccion": info["direccion"],
        "comuna": info.get("comuna", ""),
        "contacto": info["contacto"],
        "email": info["email"],
        "servicio": info["servicio_label"],
        "descripcion": "",
        "linea_servicio": "",
        "linea_medida": "",
        "linea_total": _fmt_money_clp(total_int),
        "total": _fmt_money_clp(total_int),
        "precio": _fmt_money_clp(total_int),
        "m2": "",
        "m3": "",
        "clausula_seremi": "",
    }

    if dom == "plagas":
        try:
            m2_val = float(info.get("m2", 0))
            m2_txt = str(int(m2_val)) if float(m2_val).is_integer() else str(m2_val)
        except Exception:
            m2_txt = str(info.get("m2", "")) or ""
        ctx["m2"] = m2_txt
        ctx["linea_servicio"] = info["servicio_label"]
        ctx["linea_medida"] = m2_txt
        ctx["descripcion"] = f"{info['servicio_label']}" + (f" — {m2_txt} m²" if m2_txt else "")
        ctx["clausula_seremi"] = " — con instalación de estaciones cebaderas y entrega de informe sanitario conforme a exigencias SEREMI."

    elif dom == "piscinas":
        m3_val = _volumen_estimado_m3(info)
        m3_txt = (str(int(m3_val)) if (m3_val and float(m3_val).is_integer()) else (str(m3_val) if m3_val else ""))
        try:
            m2_val = float(info.get("m2") or 0)
            m2_txt = str(int(m2_val)) if float(m2_val).is_integer() else str(m2_val)
        except Exception:
            m2_txt = str(info.get("m2", "")) or ""
        ctx["m2"] = m2_txt
        ctx["m3"] = m3_txt
        ctx["linea_servicio"] = info["servicio_label"]
        partes = []
        if m2_txt: partes.append(f"{m2_txt} m²")
        if m3_txt: partes.append(f"{m3_txt} m³")
        ctx["linea_medida"] = " — ".join(partes)
        ctx["descripcion"] = info["servicio_label"] + (f" — {ctx['linea_medida']}" if partes else "")
        ctx["clausula_seremi"] = ""

    elif dom == "camaras":
        tot, tipo, qty, unit_ap, area = calcular_total_camaras(
            info.get("tipo_camara", ""), info.get("area_vigilar", ""), info.get("cantidad_camara", "")
        )
        ctx["total"] = _fmt_money_clp(tot)
        ctx["precio"] = _fmt_money_clp(tot)
        ctx["linea_total"] = _fmt_money_clp(tot)
        ctx["linea_servicio"] = f"Cámaras {tipo} ({area})"
        ctx["linea_medida"] = f"x {qty}"
        ctx["descripcion"] = f"{info.get('tipo_camara','')} ({area}) x {qty} — {_fmt_money_clp(unit_ap)} c/u"
        ctx["clausula_seremi"] = ""
    else:
        ctx["linea_servicio"] = info["servicio_label"]
        ctx["linea_medida"] = ""
        ctx["descripcion"] = info["servicio_label"]
        ctx["clausula_seremi"] = ""

    tpl = DocxTemplate(tpl_path)
    tpl.render(ctx)
    tpl.save(path)
    return tpl_path

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
    email     = _safe(data.get("correoelect")     or data.get("email"))

    profundidad    = _safe(data.get("profundidad"))
    tamano_piscina = _safe(data.get("tamano_piscina") or data.get("tamaño_piscina"))
    m3_explicit    = _safe(data.get("m3") or data.get("volumen") or data.get("volumen_m3"))
    servicio_precio_override = _safe(data.get("servicio_precio"))

    try:
        m2_num = float((m2_raw or "0").lower().replace("m2","").replace("m²","").replace(",",".").strip() or "0")
    except Exception:
        m2_num = 0.0

    to_wa = ""
    fono = _safe(data.get("fono") or data.get("telefono") or data.get("phone"))
    if fono:
        digits = "".join(ch for ch in fono if ch.isdigit())
        if   digits.startswith("56"): to_wa = f"whatsapp:+{digits}"
        elif len(digits) == 9:        to_wa = f"whatsapp:+56{digits}"
        elif digits:                  to_wa = f"whatsapp:+{digits}"

    servicio_label  = servicio or "Desinsectación"
    servicio_precio = servicio_precio_override or _canon_servicio_para_precios(servicio_label)

    info = {
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
        "to_whatsapp": to_wa,
        "profundidad": profundidad,
        "tamano_piscina": tamano_piscina,
    }

    try:
        if m3_explicit:
            info["m3"] = float(str(m3_explicit).replace(",", "."))
    except Exception:
        pass

    return info

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

    if (docx2pdf_convert is None) and (not _lo_bin()):
        return jsonify(ok=False, error="pdf_engine_missing",
                       detail="No hay Word/docx2pdf ni LibreOffice disponibles para convertir a PDF."), 500

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    uid = uuid.uuid4().hex[:6]
    base = f"cotizacion_{ts}_{uid}"
    docx_name, pdf_name = base + ".docx", base + ".pdf"
    docx_path, pdf_path = os.path.join(FILES_DIR, docx_name), os.path.join(FILES_DIR, pdf_name)

    try:
        tpl_used = generar_docx_desde_plantilla(docx_path, info)
        app.logger.info(f"[DOCX] generado con plantilla: {tpl_used} -> {docx_path}")
        convertir_docx_a_pdf(docx_path, pdf_path)
        app.logger.info(f"[PDF] generado: {pdf_path}")
    except Exception as e:
        app.logger.exception("doc_generate_failed")
        return jsonify(
            ok=False,
            error="doc_generate_failed",
            detail=str(e),
            tpl_used=locals().get("tpl_used"),
            paths={"docx": docx_path, "pdf": pdf_path},
        ), 500

    docx_url, pdf_url = build_urls(docx_name, pdf_name)
    total_int = precio_total(info)
    total = _fmt_money_clp(total_int)

    dominio = _dominio_servicio(info.get("servicio_label",""))
    medidas_line = ""; detalle_line = ""
    if dominio == "piscinas":
        vol = _volumen_estimado_m3(info)
        base_m2 = info.get('m2',0)
        medidas_line = f"*Superficie:* {base_m2} m²" + (f" | *Volumen:* {vol} m³" if vol > 0 else "") + "\n"
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

    dbg = {
        "dominio": dominio,
        "precio_key_piscina": (info.get("servicio_precio") if info.get("servicio_precio") in PRECIOS_PISCINA else _canon_piscina_key(info.get("servicio_label",""))),
        "m3_calc": _volumen_estimado_m3(info),
        "tpl_used": tpl_used
    }

    return jsonify(ok=True, resumen=resumen, docx_url=docx_url, pdf_url=pdf_url,
                   to_wa=info.get("to_whatsapp",""), twilio=sids, dbg=dbg), 200

# -----------------------------------------------------------------------------
# Helpers para WhatsApp (entrada clave:valor)
# -----------------------------------------------------------------------------
def _parse_kv_text(msg: str) -> dict:
    """
    Parsea mensajes tipo:
      servicio: Piscinas - Plan Intermedio; m2: 56; profundidad: 1.4; telefono: +569...; email: a@b.cl
    Keys a minúsculas y sin tildes. Separadores ';' o saltos de línea.
    """
    if not msg:
        return {}
    parts = re.split(r"[;\n]+", msg)
    out = {}
    for p in parts:
        if ":" not in p:
            continue
        k, v = p.split(":", 1)
        k = _norm(k)
        v = v.strip()
        aliases = {
            "telefono": "telefono", "tel": "telefono", "fono": "fono", "phone": "phone",
            "correo": "email", "mail": "email", "e-mail": "email",
            "metros2": "m2", "metro_2": "m2",
            "tamano_piscina": "tamano_piscina", "tamaño_piscina": "tamano_piscina",
            "servicioinicial": "servicio", "servicio_inicial": "servicio",
        }
        out[aliases.get(k, k)] = v
    return out

def _flow_reset():
    return {
        "step": "servicio",
        "info": {
            "cliente": "Residencial"
        }
    }

def _prompt_for(step: str) -> str:
    prompts = {
        "servicio":  "¿Qué servicio necesitas? (ej: *Piscinas - Plan Intermedio*, *Desratización*, *Cámaras - Inalámbricas*)",
        "m2":        "¿Cuál es la *superficie en m²*? (ej: 56)",
        "profundidad":"Para piscinas, ¿profundidad en *metros*? (ej: 1.4). Escribe *omitir* si no aplica.",
        "direccion": "¿Dirección exacta?",
        "comuna":    "¿Comuna?",
        "contacto":  "¿Nombre de contacto?",
        "email":     "¿Email de contacto?",
        "telefono":  "¿Teléfono (con código país, ej: +569xxxxxxxx)?",
    }
    return prompts.get(step, "OK")

def _needs_depth(serv_label: str) -> bool:
    return "piscin" in _norm(serv_label)

def _complete(info: dict) -> bool:
    must = ["servicio_label","cliente","m2","direccion","contacto","email"]
    for k in must:
        if not info.get(k) and info.get(k) != 0:
            return False
    if _needs_depth(info.get("servicio_label","")):
        # profundidad puede omitirse pero la intentamos pedir 1 vez
        pass
    return True

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
def health():
    try:
        tdir = os.path.join(BASE_DIR, "templates")
        odir = FILES_DIR
        t_listing = sorted(os.listdir(tdir)) if os.path.isdir(tdir) else []
        o_listing = sorted(os.listdir(odir)) if os.path.isdir(odir) else []
    except Exception as e:
        t_listing, o_listing = [f"error: {e}"], []

    lo_ok = bool(_lo_bin())
    engine = "docx2pdf" if docx2pdf_convert is not None else ("libreoffice" if lo_ok else "none")
    return jsonify({
        "ok": True,
        "service": "smartplagas-bot",
        "time": datetime.datetime.utcnow().isoformat()+"Z",
        "base_url": public_base_from_request(),
        "templates_dir": tdir,
        "templates_listing": t_listing,
        "out_dir": odir,
        "out_listing": o_listing,
        "pdf_engine": engine,
    }), 200

@app.get("/whoami")
def whoami():
    return jsonify({
        "app": "smartplagas-bot",
        "version": APP_VERSION,
        "routes": ["/", "/whoami", "/health", "/generate", "/upload", "/files/<name>", "/webhook", "/reload-flow"]
    }), 200

@app.route("/files/<path:filename>")
def files(filename): return send_from_directory(FILES_DIR, filename, as_attachment=False)

# -----------------------------------------------------------------------------
# /generate (REST)
# -----------------------------------------------------------------------------
@app.post("/generate")
def generate(): return handle_generate()

# -----------------------------------------------------------------------------
# /upload único (con token)
# -----------------------------------------------------------------------------
UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "").strip()

@app.route("/upload", methods=["POST", "OPTIONS"])
def upload_pdf():
    if request.method == "OPTIONS":
        return ("", 204)

    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        token = request.headers.get("X-Upload-Token", "").strip()
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return jsonify(ok=False, error="unauthorized"), 401

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

# =====================[ FLUJO: chatbot-flujo.json ]=====================

FLOW_JSON_PATH = os.getenv("FLOW_JSON_PATH", os.path.join(BASE_DIR, "chatbot-flujo.json"))

def _flow_load():
    """Carga e indexa por id (string)."""
    try:
        with open(FLOW_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        by_id = {}
        for n in data:
            nid = str(n.get("id"))
            n["id"] = nid
            # normalizar nextId a string si existe
            if "nextId" in n and n["nextId"] not in (None, ""):
                n["nextId"] = str(n["nextId"])
            # normalizar options nextId
            for opt in n.get("options", []) or []:
                if "nextId" in opt and opt["nextId"] not in (None, ""):
                    opt["nextId"] = str(opt["nextId"])
            by_id[nid] = n
        return by_id
    except Exception as e:
        app.logger.error(f"FLOW load error: {e}")
        return {}

_FLOW = _flow_load()

def _flow_start_id():
    """Primer nodo del flujo (el más bajo) o el primero de tipo 'mensaje'."""
    if not _FLOW:
        return None
    # preferimos el de saludo si existe
    for n in _FLOW.values():
        if n.get("type") == "mensaje":
            return n["id"]
    # fallback: el menor id
    return sorted(_FLOW.keys())[0]

def _fmt_vars(text, vars_):
    if not text:
        return ""
    try:
        return text.format(**vars_)
    except Exception:
        # si falta una variable, no fallamos
        return text

def _send_menu(resp, node):
    # Devuelve el texto del menú enumerado
    lines = [node.get("content", "").strip()]
    opts = node.get("options", []) or []
    for i, o in enumerate(opts, start=1):
        # Mantenemos el texto exacto definido en el JSON
        lines.append(f"{i}. {o.get('text','').strip()}")
    msg = "\n".join(lines).strip()
    if msg:
        resp.message(msg)

def _try_pick_option(node, user_text):
    """Interpreta la respuesta del usuario contra las options del node."""
    opts = node.get("options", []) or []
    txt = (user_text or "").strip().lower()

    # 1) si es número 1..N
    m = re.match(r"^\s*(\d+)\s*$", txt)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(opts):
            return opts[idx]

    # 2) match por texto (sin tildes / casefold)
    def norm(s):
        s = s or ""
        s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
        return re.sub(r"\s+", " ", s).strip().lower()

    txtn = norm(txt)
    for o in opts:
        if norm(o.get("text", "")) == txtn:
            return o

    return None

def _flow_emit_until_input(resp, sess):
    """
    Emite todos los 'mensaje' encadenados y se detiene en el primer 'pregunta' o 'condicional'.
    Guarda en sesión el node_id donde esperar input.
    """
    current = sess.get("node_id") or _flow_start_id()
    vars_ = sess.get("vars", {})

    visited = set()
    while current and current in _FLOW and current not in visited:
        visited.add(current)
        node = _FLOW[current]
        ntype = node.get("type")

        if ntype == "mensaje":
            txt = _fmt_vars(node.get("content", ""), vars_)
            if txt:
                resp.message(txt)
            current = node.get("nextId")
            continue

        elif ntype == "pregunta":
            # hacemos la pregunta y paramos aquí a esperar respuesta
            txt = _fmt_vars(node.get("content", ""), vars_)
            if txt:
                resp.message(txt)
            sess["node_id"] = node["id"]
            return

        elif ntype == "condicional":
            # enviamos menú y paramos a esperar respuesta
            _send_menu(resp, node)
            sess["node_id"] = node["id"]
            return

        else:
            # tipo desconocido -> avanzamos si hay next
            current = node.get("nextId")

    # si salimos sin encontrar input, cerramos sesión (flujo terminado)
    sess["node_id"] = None


def _map_rango_m2_to_number(rango: str) -> int:
    if not rango:
        return 0
    s = rango.lower()
    if "menos" in s or "<" in s or "100" in s and "200" not in s:
        return 90
    if "100" in s and "200" in s:
        return 150
    if "200" in s or "más" in s or "mas" in s or ">" in s:
        return 250
    return 0

def _compose_payload_from_vars(vars_, from_wa: str):
    """
    Construye el payload para generar la cotización desde las variables del flujo.
    """
    servicio = vars_.get("servicio", "")  # puede ser “Piscinas”, “Control de Plagas”, “Cámaras…”
    subservicio = vars_.get("subservicio", "")
    direccion = vars_.get("direccion", "")
    comuna = vars_.get("comuna", "")
    email = vars_.get("email", "")
    telefono = vars_.get("telefono", "")
    nombre = vars_.get("nombre", "")

    # m2: puede venir por rango (rango_m2)
    m2 = _map_rango_m2_to_number(vars_.get("rango_m2", ""))

    # Piscinas
    tamano_piscina = vars_.get("tamano_piscina", "")
    profundidad = vars_.get("profundidad", "")

    # Cámaras
    tipo_camara = vars_.get("tipo_camara", "")
    cantidad_camara = vars_.get("cantidad_camara", "")
    area_vigilar = vars_.get("area_vigilar", "")

    # Servicio legible
    servicio_label = ""
    if "piscin" in servicio.lower():
        servicio_label = subservicio or "Piscinas"
    elif "cámara" in servicio.lower() or "camara" in servicio.lower():
        servicio_label = "Cámaras"
    else:
        # Control de plagas
        if subservicio:
            servicio_label = subservicio
        else:
            servicio_label = "Control de Plagas"

    # Payload compatible con normalize_payload()
    payload = {
        "servicio": servicio_label,
        "tipo_clientes": "Residencial",
        "m2": m2,
        "direccion": direccion,
        "comuna": comuna,
        "contacto": nombre,
        "email": email,
        "phone": telefono,   # si viene vacío, usaremos el From de WhatsApp
        # Piscinas
        "tamano_piscina": tamano_piscina,
        "profundidad": profundidad,
        # Cámaras
        "tipo_camara": tipo_camara,
        "cantidad_camara": cantidad_camara,
        "area_vigilar": area_vigilar,
    }

    # si el usuario no escribió teléfono, usamos el del webhook “From”
    if not payload.get("phone") and from_wa:
        # llega como "whatsapp:+569xxxxxxx"
        payload["phone"] = from_wa.replace("whatsapp:", "")

    return payload

def _flow_finish_and_generate(resp, form, sess):
    """
    Cuando el flujo termina, armamos el payload, generamos PDF/DOCX y enviamos al mismo usuario por WhatsApp.
    Reutiliza la misma lógica de generación que /generate.
    """
    vars_ = sess.get("vars", {})
    from_wa = form.get("From") or ""
    payload = _compose_payload_from_vars(vars_, from_wa)

    # Normalizamos igual que /generate
    info = normalize_payload(payload)

    # Motor PDF disponible?
    if (docx2pdf_convert is None) and (not _lo_bin()):
        resp.message("⚠️ No pude generar PDF por un problema interno de conversión. Intentaremos de nuevo pronto.")
        return

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    uid = uuid.uuid4().hex[:6]
    base = f"cotizacion_{ts}_{uid}"
    docx_name, pdf_name = base + ".docx", base + ".pdf"
    docx_path, pdf_path = os.path.join(FILES_DIR, docx_name), os.path.join(FILES_DIR, pdf_name)

    try:
        tpl_used = generar_docx_desde_plantilla(docx_path, info)
        convertir_docx_a_pdf(docx_path, pdf_path)
    except Exception as e:
        app.logger.exception("gen-fail")
        resp.message("⚠️ No pude generar la cotización. Intenta nuevamente.")
        return

    docx_url, pdf_url = build_urls(docx_name, pdf_name)
    total_int = precio_total(info)
    total = _fmt_money_clp(total_int)

    # Resumen para el cliente
    resumen = (
        "✅ Cotización lista\n"
        f"• Servicio: {info.get('servicio_label','')}\n"
        f"• Total: {total}\n"
        f"• PDF: {pdf_url}"
    )
    # Enviamos el PDF como media + resumen (y dejamos el texto con el link por si el cliente no ve el adjunto)
    try:
        # Mensaje con media
        if twilio and TWILIO_ENABLED and from_wa:
            twilio.messages.create(from_=TW_FROM, to=from_wa, body=resumen, media_url=[pdf_url])
        else:
            resp.message(resumen)
    except Exception:
        # fallback: al menos enviamos el texto
        resp.message(resumen)

    # Limpiamos sesión para no quedar esperando entradas
    sess["node_id"] = None
    _sess_set(_sess_key(form), sess)

# =========================[ WEBHOOK con flujo ]=========================

@app.route("/webhook", methods=["GET", "POST", "HEAD"])
def webhook():
    if request.method != "POST":
        return "ok", 200, {"Content-Type": "text/plain"}

    form = request.form.to_dict() if not request.is_json else (request.get_json() or {})
    body = (form.get("Body") or "").strip()
    body_lc = body.lower()
    msg_sid = (form.get("MessageSid") or "").strip()

    # anti duplicados
    if not _dedup_should_process(msg_sid):
        return str(MessagingResponse()), 200, {"Content-Type": "application/xml"}

    resp = MessagingResponse()
    sess_id = _sess_key(form) or "anon"
    sess = _sess_get(sess_id) or {"node_id": _flow_start_id(), "vars": {}}

    # Comandos de reinicio
    if body_lc in {"reiniciar", "reset", "start", "hola", "buenas", "buenos dias", "buenas tardes", "buenas noches"}:
        sess = {"node_id": _flow_start_id(), "vars": {}}
        _flow_emit_until_input(resp, sess)
        _sess_set(sess_id, sess)
        return str(resp), 200, {"Content-Type":"application/xml"}

    # Si no hay flujo cargado, comportarnos como antes
    if not _FLOW or not sess.get("node_id"):
        resp.message("🤖 Endpoint activo. Usa /generate (POST JSON) para cotizar por REST.")
        return str(resp), 200, {"Content-Type":"application/xml"}

    current_id = sess.get("node_id")
    node = _FLOW.get(current_id)

    if not node:
        # flujo inconsistente -> reiniciamos
        sess = {"node_id": _flow_start_id(), "vars": {}}
        _flow_emit_until_input(resp, sess)
        _sess_set(sess_id, sess)
        return str(resp), 200, {"Content-Type":"application/xml"}

    vars_ = sess.get("vars", {})

    # Procesamos la respuesta del usuario según el tipo del nodo actual
    if node.get("type") == "pregunta":
        varname = node.get("variableName", "").strip()
        if varname:
            vars_[varname] = body
        next_id = node.get("nextId")
        sess["vars"] = vars_
        sess["node_id"] = next_id

    elif node.get("type") == "condicional":
        chosen = _try_pick_option(node, body)
        if not chosen:
            # no entendimos la opción -> re-enviar menú
            _send_menu(resp, node)
            _sess_set(sess_id, sess)
            return str(resp), 200, {"Content-Type":"application/xml"}

        save_as = chosen.get("saveAs")
        if save_as:
            vars_[save_as] = chosen.get("text", "")
        sess["vars"] = vars_
        sess["node_id"] = chosen.get("nextId") or node.get("nextId")

    else:
        # si el nodo que espera input no es válido, avanzamos
        sess["node_id"] = node.get("nextId")

    # Emitimos hasta el próximo input
    _flow_emit_until_input(resp, sess)

    # ¿Terminó el flujo? (node_id None o no existe)
    if not sess.get("node_id"):
        # Enviar mensaje de “gracias” final del JSON si existe
        # (el flujo ya lo debió enviar en _flow_emit_until_input; aquí generamos y enviamos la cotización)
        _flow_finish_and_generate(resp, form, sess)

    # Guardar sesión
    _sess_set(sess_id, sess)
    return str(resp), 200, {"Content-Type":"application/xml"}

# -----------------------------------------------------------------------------
@app.post("/reload-flow")
def reload_flow():
    try:
        return jsonify(ok=True, count=0), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

def _log_url_map():
    try: logging.info("URL MAP:\n%s", app.url_map)
    except Exception: pass

_log_url_map()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True, use_reloader=False)
