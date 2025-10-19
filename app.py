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

# Profundidad por defecto para piscinas (si el usuario no la entrega)
POOL_DEFAULT_DEPTH = float(os.getenv("POOL_DEFAULT_DEPTH", "1.4"))
# Volumen mínimo asumido en piscinas cuando no hay m2/m3
PISCINA_MIN_M3_DEFAULT = float(os.getenv("PISCINA_MIN_M3_DEFAULT", "30"))

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

# === NUEVO: reenvío (reflejo) a números administradores ===
# FORWARD_TO_NUMBERS debe ser números E.164 sin "whatsapp:" (ej: +56995300790, +56958XXXXXX)
FWD_LIST = [n.strip() for n in (os.getenv("FORWARD_TO_NUMBERS", "") or "").split(",") if n.strip()]
FWD_ON   = os.getenv("FORWARD_ENABLE", "1") not in ("0", "false", "False", "")

BASE_URL = (os.getenv("BASE_URL") or os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
FILES_SUBDIR = (os.getenv("FILES_DIR", "out") or "out").strip()
FILES_DIR    = os.path.join(BASE_DIR, FILES_SUBDIR)
os.makedirs(FILES_DIR, exist_ok=True)

# Directorios donde buscaremos plantillas
TEMPLATE_DIRS = [
    os.getenv("TEMPLATES_DIR", "").strip(),
    os.path.join(BASE_DIR, "templates"),
    "/app/templates",
    "/templates",
    FILES_DIR,  # por si se subió vía /upload
]

# Envíos
SEND_PDF    = (os.getenv("SEND_PDF_TO_CLIENT", "true").lower() == "true")
SEND_DOC    = (os.getenv("SEND_DOC_TO_CLIENT", "false").lower() == "true")
MEDIA_DELAY = float(os.getenv("MEDIA_DELAY_SECONDS", "1.0"))
SEND_COPY_TO_ADMIN = (os.getenv("SEND_COPY_TO_ADMIN", "true").lower() == "true")

twilio = Client(TW_SID, TW_TOKEN) if (TW_SID and TW_TOKEN) else None

# ======== FUNCIONES NUEVAS: REFLEJO DE MENSAJES =========
def _safe_text(s: str, maxlen: int = 1200) -> str:
    try:
        s = s or ""
        return (s[:maxlen] + "…") if len(s) > maxlen else s
    except Exception:
        return ""

def forward_incoming_to_owners(wa_from: str, body: str, media_items: list):
    """
    Envía copia del mensaje entrante (texto y/o medios) a cada número en FWD_LIST.
    No interfiere con el flujo normal del bot.
    """
    if not (FWD_ON and twilio and TWILIO_ENABLED and TW_FROM and FWD_LIST):
        return
    header = f"📩 *Nuevo mensaje a Smart Plagas*\nDe: {wa_from}\n"
    text = _safe_text(body)
    caption = header + ("\n— — —\n" + text if text else "")

    # 1) texto
    try:
        if text.strip():
            for to in FWD_LIST:
                to_wa = f"whatsapp:{to}" if not to.startswith("whatsapp:") else to
                twilio.messages.create(from_=TW_FROM, to=to_wa, body=caption)
        else:
            for to in FWD_LIST:
                to_wa = f"whatsapp:{to}" if not to.startswith("whatsapp:") else to
                twilio.messages.create(from_=TW_FROM, to=to_wa, body=header)
    except Exception as e:
        logging.exception(f"[FORWARD:text] {e}")

    # 2) medias
    try:
        for i, m in enumerate(media_items or [], start=1):
            media_url = m.get("url")
            ctype = m.get("content_type", "")
            foot = f"{header}\n📎 Archivo {i} ({ctype})"
            for to in FWD_LIST:
                to_wa = f"whatsapp:{to}" if not to.startswith("whatsapp:") else to
                twilio.messages.create(from_=TW_FROM, to=to_wa, body=foot, media_url=[media_url] if media_url else None)
    except Exception as e:
        logging.exception(f"[FORWARD:media] {e}")
# ========================================================

# -----------------------------------------------------------------------------
# Precios y utilidades
# -----------------------------------------------------------------------------
# Tramos por m² (plagas)
TRAMOS = [(0,50),(51,100),(101,200),(201,300),(301,500),(501,1000),(1001,2000),(2001,9999999)]

# 🔁 NUEVO: claves detalladas por servicio + subárea (interior / exterior / interior y exterior)
PRECIOS = {
    "desinsectacion interior":             [33600, 42750, 58500,  72000,  94500, 148500, 243000, 396000],
    "desinsectacion exterior":             [22400, 28500, 39000,  48000,  63000,  99000, 162000, 264000],
    "desinsectacion interior y exterior":  [56000, 71250, 97500, 120000, 157500, 247500, 405000, 660000],

    "desratizacion interior":              [30600, 39600, 54000,  67500,  87750, 135000, 211500, 337500],
    "desratizacion exterior":              [20400, 26400, 36000,  45000,  58500,  90000, 141000, 225000],
    "desratizacion interior y exterior":   [51000, 66000, 90000, 112500, 146250, 225000, 352500, 562500],
}

# Tramos por m³ (piscinas)
TRAMOS_M3 = [(0,25),(26,50),(51,100),(101,999999)]
PRECIOS_PISCINA = {
    "piscina_plan_intermedio_m3":  [4400, 3800, 3500, 3250],      # $/m³
    "piscina_mantencion_bomba_m3": [3600, 3350, 3150, 2900],      # $/m³
    "piscina_shock_m3":            [1700, 1450, 1250, 1000],      # $/m³
    "piscina_diagnostico_total":   [34000, 39000, 45000, 50500],  # total
    "piscina_cambio_arena_total":  [101000,157000,224000,336000], # total
}

# Cámaras (unitarios)
CAM_PRECIOS = {
    "alambricas":   {"interior":77000,"exterior":99000},
    "inalambricas": {"interior":66000,"exterior":88000},
    "solares":      {"exterior":165000},
    "dvr":          {"interior":82500,"exterior":104500},
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
# -----------------------------------------------------------------------------
# Normalizaciones y Aliases de servicio/subárea
# -----------------------------------------------------------------------------
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

def _normalize_txt(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = " ".join(s.split())
    return s

# Base → "desinsectacion" | "desratizacion"
def _canon_base_plaga(servicio_humano: str) -> str:
    s = _strip_accents_and_symbols(servicio_humano)
    if "ratiz" in s:  return "desratizacion"
    if "infecc" in s: return "desinfeccion"  # (si alguna vez lo reactivas)
    return "desinsectacion"

# Subárea → "interior" | "exterior" | "interior y exterior"
def _canon_subarea(subarea_humano: str) -> str:
    s = _normalize_txt(subarea_humano)
    if not s: return ""
    if s in {"interior"}: return "interior"
    if s in {"exterior"}: return "exterior"
    if s in {"ambas","ambos","completo","interior y exterior","interior + exterior","interior exterior"}:
        return "interior y exterior"
    # heurística por palabras
    if "interior" in s and "exterior" in s: return "interior y exterior"
    if "interior" in s: return "interior"
    if "exterior" in s: return "exterior"
    if "amb" in s or "complet" in s: return "interior y exterior"
    return ""

# Clave final para PRECIOS
def servicio_clave_plaga(servicio_humano: str, subarea_humano: str) -> str:
    base = _canon_base_plaga(servicio_humano)
    area = _canon_subarea(subarea_humano) or "interior y exterior"
    clave = f"{base} {area}"
    return clave

# --- Helpers Cámaras ---
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

# --- Piscinas: m2 y m3 ---
def parse_pool_size_to_m2(size_text: str) -> float:
    if not size_text:
        return 0.0
    s = str(size_text).lower()
    s = s.replace("metros", "").replace("metro", "")
    s = s.replace("m2", "").replace("m²", "").replace("mts", "").replace("mt", "").replace("m", "")
    s = s.replace(",", ".").strip()
    s = s.replace("por", "x").replace("*", "x")
    s = re.sub(r"\s+", "", s)
    m = re.match(r"^(\d+(?:\.\d+)?)[x×](\d+(?:\.\d+)?)$", s)
    if m:
        try:
            a = float(m.group(1)); b = float(m.group(2))
            return round(a * b, 2)
        except Exception:
            return 0.0
    m2m = re.match(r"^(\d+(?:\.\d+)?)$", s)
    if m2m:
        return float(m2m.group(1))
    return 0.0
def idx_tramo_por_m2(m2: float) -> int:
    try:
        val = float(m2)
    except Exception:
        raise ValueError("Los m² no son numéricos.")
    if val < 0:
        raise ValueError("Los m² no pueden ser negativos.")
    for i, (lo, hi) in enumerate(TRAMOS):
        if lo <= val <= hi:
            return i
    return len(TRAMOS) - 1

def precio_por_tramo_plaga(clave_servicio: str, m2: float) -> int:
    tabla = PRECIOS.get(clave_servicio)
    if not tabla: return 0
    i = idx_tramo_por_m2(m2 or 0)
    if len(tabla) != len(TRAMOS):
        raise RuntimeError(f"Inconsistencia: {clave_servicio} tiene {len(tabla)} precios, TRAMOS={len(TRAMOS)}.")
    return int(tabla[i])

# Piscinas
def _volumen_estimado_m3(info: dict) -> float:
    for k in ("m3","volumen","volumen_m3"):
        v = str(info.get(k, "") or "").strip()
        if v:
            try: return float(v.replace(",", "."))
            except Exception: pass
    try: m2 = float(info.get("m2") or 0)
    except Exception: m2 = 0.0
    prof_raw = info.get("profundidad")
    prof_val = None
    if prof_raw not in (None, ""):
        try:
            prof_val = float(str(prof_raw).replace(",", "."))
        except Exception:
            prof_val = None
    if m2 > 0:
        depth = prof_val if (prof_val is not None and prof_val > 0) else POOL_DEFAULT_DEPTH
        return round(m2 * depth, 1)
    return 0.0

def _precio_piscina_por_tramo(serv_key: str, m3: float) -> int:
    tabla = PRECIOS_PISCINA.get(serv_key)
    if not tabla: return 0
    if serv_key.endswith("_m3") and (m3 is None or m3 <= 0):
        m3 = PISCINA_MIN_M3_DEFAULT
    idx = len(TRAMOS_M3) - 1
    for i, (lo, hi) in enumerate(TRAMOS_M3):
        if lo <= m3 <= hi: idx = i; break
    if serv_key.endswith("_m3"):
        unit = tabla[idx]
        return int(round(unit * m3))
    return int(tabla[idx] or 0)

def precio_total(info: dict) -> int:
    dominio = _dominio_from_info(info)
    if dominio == "piscinas":
        label = info.get("servicio_label", "")
        key = _canon_piscina_key(label) or "piscina_plan_intermedio_m3"
        m3 = _volumen_estimado_m3(info)
        if key.endswith("_m3") and (m3 is None or m3 <= 0):
            m3 = PISCINA_MIN_M3_DEFAULT
            info["__m3_asumido__"] = True
            info["__m3_asumido_val__"] = m3
        return _precio_piscina_por_tramo(key, m3)
    if dominio == "plagas":
        clave = servicio_clave_plaga(info.get("servicio_label",""), info.get("subarea","") or info.get("subservicio_area",""))
        return precio_por_tramo_plaga(clave, info.get("m2") or 0)
    if dominio == "camaras":
        total, _, _, _, _ = calcular_total_camaras(
            info.get("tipo_camara",""), info.get("area_vigilar",""), info.get("cantidad_camara","")
        )
        return total
    return 0

# Dominio y piscina key
def _canon_piscina_key(label: str) -> str:
    s = _norm(label)
    if "plan intermedio" in s or ("tratamient" in s and "limpiez" in s): return "piscina_plan_intermedio_m3"
    if ("bomba" in s) or ("filtro" in s) or ("mantencion" in s):         return "piscina_mantencion_bomba_m3"
    if ("shock" in s) or ("clor" in s):                                   return "piscina_shock_m3"
    if ("diagn" in s):                                                    return "piscina_diagnostico_total"
    if ("arena" in s) or ("carga" in s):                                  return "piscina_cambio_arena_total"
    return ""

def _dominio_from_info(info: dict) -> str:
    label = info.get("servicio_label","")
    s = _norm(label)
    if _canon_piscina_key(label): return "piscinas"
    if "piscin" in s:             return "piscinas"
    if info.get("tamano_piscina") or info.get("profundidad") or ("m3" in info):
        return "piscinas"
    if "camar" in s or info.get("tipo_camara") or info.get("cantidad_camara"):
        return "camaras"
    if any(k in s for k in ("plaga","desratiz","desinsect","sanitiz")):
        return "plagas"
    return "otro"

# --- Normalización de payload externo y generate ---
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

# Mostrar "desde" = primer tramo del servicio elegido
def precio_desde_prim_tramo(clave_servicio: str) -> int:
    tabla = PRECIOS.get(clave_servicio)
    if not tabla: return 0
    return int(tabla[0])

# --- DOCX -> PDF ---
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
# Selección de plantilla + Render DOCX
# -----------------------------------------------------------------------------
def _select_template_path(info: dict) -> str:
    dom = _dominio_from_info(info) or "otro"
    por_dom = {
        "plagas":   ["cotizacion_plagas.docx",   "templatescotizacion_plagas.docx"],
        "piscinas": ["cotizacion_piscinas.docx", "templatescotizacion_piscinas.docx"],
        "camaras":  ["cotizacion_camaras.docx",  "templatescotizacion_camaras.docx"],
    }
    prefer = por_dom.get(dom, []) + [
        "cotizacion_template.docx", "templatescotizacion_template.docx",
        "Plantilla_Cotizacion.docx"
    ]
    for d in TEMPLATE_DIRS:
        if not d: continue
        for name in prefer:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                app.logger.info(f"[TPL] Usando plantilla preferida: {p}")
                return p
    for d in TEMPLATE_DIRS:
        if d and os.path.isdir(d):
            for fname in os.listdir(d):
                if fname.lower().endswith(".docx") and "template" in fname.lower():
                    return os.path.join(d, fname)
    for d in TEMPLATE_DIRS:
        if d and os.path.isdir(d):
            for fname in os.listdir(d):
                if fname.lower().endswith(".docx"):
                    return os.path.join(d, fname)
    raise FileNotFoundError("No se encontraron plantillas DOCX")

def generar_docx_desde_plantilla(path: str, info: dict) -> str:
    tpl_path = _select_template_path(info)
    if not os.path.exists(tpl_path):
        raise FileNotFoundError(f"Plantilla no encontrada: {tpl_path}")

    dom = _dominio_from_info(info)

    # Calcula total inicial (y setea flags de m3 asumido si aplica)
    total_int = precio_total(info)

    # Contexto base para la plantilla DOCX
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

    # -------------------- PLAGAS --------------------
    if dom == "plagas":
        try:
            m2_val = float(info.get("m2", 0))
            m2_txt = str(int(m2_val)) if float(m2_val).is_integer() else str(m2_val)
        except Exception:
            m2_txt = str(info.get("m2", "")) or ""
        ctx["m2"] = m2_txt

        # Línea de servicio + subárea (si existe)
        ctx["linea_servicio"] = info["servicio_label"] + (f" — {info.get('subarea','')}" if info.get("subarea") else "")

        # Medida
        ctx["linea_medida"] = m2_txt if m2_txt else "1"

        # Descripción
        ctx["descripcion"] = f"{info['servicio_label']}" + (f" — {m2_txt} m²" if m2_txt else "")

        # Clausula SEREMI SOLO para DESRATIZACIÓN
        base_serv = _canon_base_plaga(info["servicio_label"])  # 'desratizacion' | 'desinsectacion' | 'desinfeccion'
        if base_serv == "desratizacion":
            ctx["clausula_seremi"] = " — con instalación de estaciones cebaderas y entrega de informe sanitario conforme a exigencias SEREMI."
        else:
            ctx["clausula_seremi"] = ""

    # -------------------- PISCINAS --------------------
    elif dom == "piscinas":
        try:
            m2_val = float(info.get("m2") or 0)
        except Exception:
            m2_val = 0.0

        label = info.get("servicio_label", "")
        key = _canon_piscina_key(label) or "piscina_plan_intermedio_m3"
        m3_val = _volumen_estimado_m3(info)

        # Fallback para m3 si el servicio es por m3
        if key.endswith("_m3") and (m3_val is None or m3_val <= 0):
            m3_val = info.get("__m3_asumido_val__", PISCINA_MIN_M3_DEFAULT)
            info["__m3_asumido__"] = True
            info["__m3_asumido_val__"] = m3_val

        total_int = _precio_piscina_por_tramo(key, m3_val)

        ctx["precio"] = _fmt_money_clp(total_int)
        ctx["total"] = _fmt_money_clp(total_int)
        ctx["linea_total"] = _fmt_money_clp(total_int)

        m3_txt = str(int(m3_val)) if (m3_val and float(m3_val).is_integer()) else (str(m3_val) if m3_val else "")
        m2_txt = str(int(m2_val)) if m2_val and float(m2_val).is_integer() else (str(m2_val) if m2_val else "")
        ctx["m2"] = m2_txt
        ctx["m3"] = m3_txt

        ctx["linea_servicio"] = info["servicio_label"]
        ctx["linea_medida"] = m3_txt if m3_txt else (m2_txt if m2_txt else "1")
        ctx["descripcion"] = info["servicio_label"]
        ctx["clausula_seremi"] = ""  # no aplica en piscinas

    # -------------------- OTROS (cámaras, etc.) --------------------
    else:
        ctx["descripcion"] = info["servicio_label"]
        ctx["clausula_seremi"] = ""

    # Render y guardado del DOCX
    tpl = DocxTemplate(tpl_path)
    tpl.render(ctx)
    tpl.save(path)

    # Fin: devolver la ruta de la plantilla utilizada (como hacía tu código original)
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
        sids["admin_pdf"]  = send_whatsapp_media_only_pdf(ADMIN_WA, "📄 PDF de la cotización", pdf_url, delay=MEDIA_DELAY)
    if docx_url:
        sids["admin_docx"] = send_whatsapp_text(ADMIN_WA, f"🖹 DOCX: {docx_url}", delay=MEDIA_DELAY)
    return sids

# -----------------------------------------------------------------------------
# Normalización de payload externo y generate
# -----------------------------------------------------------------------------
def normalize_payload(data: dict) -> dict:
    data = data or {}

    servicio  = _safe(data.get("servicioinicial") or data.get("servicio") or data.get("servicio_inicial"))
    subarea   = _safe(data.get("subarea") or data.get("subservicio_area"))  # <- NUEVO: capturamos subárea del flujo
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

    # cámaras:
    tipo_camara     = _safe(data.get("tipo_camara"))
    cantidad_camara = _safe(data.get("cantidad_camara"))
    area_vigilar    = _safe(data.get("area_vigilar"))

    # m2 declarado
    try:
        m2_num = float((m2_raw or "0").lower().replace("m2","").replace("m²","").replace(",",".").strip() or "0")
    except Exception:
        m2_num = 0.0

    # si m2 no viene, intentar desde "LxA"
    if (not m2_num) and tamano_piscina:
        calc_m2 = parse_pool_size_to_m2(tamano_piscina)
        if calc_m2 > 0:
            m2_num = calc_m2

    # to whatsapp
    to_wa = ""
    fono = _safe(data.get("fono") or data.get("telefono") or data.get("phone"))
    if fono:
        digits = "".join(ch for ch in fono if ch.isdigit())
        if   digits.startswith("56"): to_wa = f"whatsapp:+{digits}"
        elif len(digits) == 9:        to_wa = f"whatsapp:+56{digits}"
        elif digits:                  to_wa = f"whatsapp:+{digits}"

    servicio_label  = servicio or "Desinsectación"

    info = {
        "fecha": datetime.date.today().strftime("%d-%m-%Y"),
        "servicio_label": servicio_label,
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
        # cámaras:
        "tipo_camara": tipo_camara,
        "cantidad_camara": cantidad_camara,
        "area_vigilar": area_vigilar,
        # NUEVO:
        "subarea": subarea,
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

    # Prefijo piscinas si aplica
    if _dominio_from_info(info) == "piscinas" and "piscin" not in _norm(info.get("servicio_label","")):
        info["servicio_label"] = f"Piscinas – {info.get('servicio_label','')}"

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

    dominio = _dominio_from_info(info)
    medidas_line = ""; detalle_line = ""
    if dominio == "piscinas":
        vol = _volumen_estimado_m3(info)
        if vol <= 0:
            vol = info.get("__m3_asumido_val__", PISCINA_MIN_M3_DEFAULT)
            medidas_line = f"*Volumen (asumido):* {vol} m³\n"
        else:
            medidas_line = f"*Volumen:* {vol} m³\n"
    elif dominio == "plagas":
        medidas_line = f"*Superficie tratada:* {info.get('m2',0)} m²\n"
        if info.get("subarea"):
            detalle_line = f"*Área:* {info.get('subarea')}\n"
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
        sids["client_pdf"] = send_whatsapp_media_only_pdf(info["to_whatsapp"], "📄 Cotización adjunta", pdf_url, MEDIA_DELAY)
        if SEND_DOC:
            send_whatsapp_text(info["to_whatsapp"], f"🖹 DOCX: {docx_url}", delay=MEDIA_DELAY)

    if SEND_COPY_TO_ADMIN and ADMIN_WA:
        sids["admin"] = send_admin_copy(resumen, pdf_url, docx_url)

    dbg = {
        "dominio": _dominio_from_info(info),
        "m3_calc": _volumen_estimado_m3(info) or info.get("__m3_asumido_val__"),
        "tpl_used": tpl_used
    }

    return jsonify(ok=True, resumen=resumen, docx_url=docx_url, pdf_url=pdf_url,
                   to_wa=info.get("to_whatsapp",""), twilio=sids, dbg=dbg), 200
# -----------------------------------------------------------------------------
# Helpers para WhatsApp (entrada clave:valor)
# -----------------------------------------------------------------------------
def _parse_kv_text(msg: str) -> dict:
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
    return {"step": "servicio", "info": {"cliente": "Residencial"}}

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
        pass
    return True

# -----------------------------------------------------------------------------
# FLUJO
# -----------------------------------------------------------------------------
FLOW_JSON_PATH = os.getenv("FLOW_JSON_PATH", os.path.join(BASE_DIR, "chatbot-flujo.json"))

def _flow_load():
    try:
        with open(FLOW_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        by_id = {}
        for n in data:
            nid = str(n.get("id"))
            n["id"] = nid
            if "nextId" in n and n["nextId"] not in (None, ""):
                n["nextId"] = str(n["nextId"])
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
    if not _FLOW:
        return None
    for n in _FLOW.values():
        if n.get("type") == "mensaje":
            return n["id"]
    return sorted(_FLOW.keys())[0]

def _fmt_vars(text, vars_):
    if not text:
        return ""
    try:
        return text.format(**vars_)
    except Exception:
        return text

def _send_menu(resp, node):
    lines = [node.get("content", "").strip()]
    opts = node.get("options", []) or []
    for i, o in enumerate(opts, start=1):
        lines.append(f"{i}. {o.get('text','').strip()}")
    msg = "\n".join(lines).strip()
    if msg:
        resp.message(msg)

def _try_pick_option(node, user_text):
    opts = node.get("options", []) or []
    txt = (user_text or "").strip().lower()
    m = re.match(r"^\s*(\d+)\s*$", txt)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(opts):
            return opts[idx]
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
    current = sess.get("node_id") or _flow_start_id()
    vars_ = sess.get("vars", {})
    visited = set()
    while current and current in _FLOW and current not in visited:
        visited.add(current)
        node = _FLOW[current]
        ntype = node.get("type")
        if ntype == "mensaje":
            txt = _fmt_vars(node.get("content", ""), vars_)
            if txt: resp.message(txt)
            current = node.get("nextId"); continue
        elif ntype == "pregunta":
            txt = _fmt_vars(node.get("content", ""), vars_)
            if txt: resp.message(txt)
            sess["node_id"] = node["id"]; return
        elif ntype == "condicional":
            _send_menu(resp, node)
            sess["node_id"] = node["id"]; return
        else:
            current = node.get("nextId")
    sess["node_id"] = None

def _map_rango_m2_to_number(rango: str) -> int:
    if not rango: return 0
    s = rango.lower()
    if "menos" in s or "<" in s or ("100" in s and "200" not in s): return 90
    if "100" in s and "200" in s: return 150
    if "200" in s or "más" in s or ">" in s: return 250
    return 0

def _compose_payload_from_vars(vars_, from_wa: str):
    servicio = vars_.get("servicio", "")
    subservicio = vars_.get("subservicio", "")
    subarea = vars_.get("subarea", "")  # <- NUEVO
    direccion = vars_.get("direccion", "")
    comuna = vars_.get("comuna", "")
    email = vars_.get("email", "")
    telefono = vars_.get("telefono", "")
    nombre = vars_.get("nombre", "")
    m2 = _map_rango_m2_to_number(vars_.get("rango_m2", ""))

    tamano_piscina = vars_.get("tamano_piscina", "")
    profundidad = vars_.get("profundidad", "")

    tipo_camara = vars_.get("tipo_camara", "")
    cantidad_camara = vars_.get("cantidad_camara", "")
    area_vigilar = vars_.get("area_vigilar", "")

    # Etiqueta visible del servicio
    if "piscin" in (servicio or "").lower():
        servicio_label = f"Piscinas – {subservicio or 'Servicio'}"
    elif "cámara" in (servicio or "").lower() or "camara" in (servicio or "").lower():
        servicio_label = f"Cámaras – {subservicio or 'Servicio'}"
    else:
        servicio_label = subservicio or "Control de Plagas"

    payload = {
        "servicio": servicio_label,
        "tipo_clientes": "Residencial",
        "m2": m2,
        "direccion": direccion,
        "comuna": comuna,
        "contacto": nombre,
        "email": email,
        "phone": telefono,
        "tamano_piscina": tamano_piscina,
        "profundidad": profundidad,
        "tipo_camara": tipo_camara,
        "cantidad_camara": cantidad_camara,
        "area_vigilar": area_vigilar,
        # NUEVO:
        "subarea": subarea,
    }

    if not payload.get("phone") and from_wa:
        payload["phone"] = from_wa.replace("whatsapp:", "")

    return payload

def _flow_finish_and_generate(resp, form, sess):
    vars_ = sess.get("vars", {})
    from_wa = form.get("From") or ""
    payload = _compose_payload_from_vars(vars_, from_wa)

    info = normalize_payload(payload)

    if _dominio_from_info(info) == "piscinas" and "piscin" not in _norm(info.get("servicio_label","")):
        info["servicio_label"] = f"Piscinas – {info.get('servicio_label','')}"

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
    except Exception:
        app.logger.exception("gen-fail")
        resp.message("⚠️ No pude generar la cotización. Intenta nuevamente.")
        return

    docx_url, pdf_url = build_urls(docx_name, pdf_name)
    total_int = precio_total(info)
    total = _fmt_money_clp(total_int)

    resumen = (
        "✅ Cotización lista\n"
        f"• Servicio: {info.get('servicio_label','')}\n"
        f"• Área: {info.get('subarea','')}\n"
        f"• Total: {total}\n"
        f"• PDF: {pdf_url}"
    )
    try:
        if twilio and TWILIO_ENABLED and from_wa:
            twilio.messages.create(from_=TW_FROM, to=from_wa, body=resumen, media_url=[pdf_url])
        else:
            resp.message(resumen)
    except Exception:
        resp.message(resumen)

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

    # Deduplicar
    if not _dedup_should_process(msg_sid):
        return str(MessagingResponse()), 200, {"Content-Type": "application/xml"}

    # === Datos para reenvío (reflejo) ===
    wa_from = form.get("From", "")
    try:
        num_media = int(form.get("NumMedia", "0"))
    except Exception:
        num_media = 0
    media_items = []
    for i in range(num_media):
        media_items.append({
            "url": form.get(f"MediaUrl{i}"),
            "content_type": form.get(f"MediaContentType{i}", "")
        })
    # >>> Enviar copia a administradores (no bloqueante del flujo)
    try:
        forward_incoming_to_owners(wa_from=wa_from, body=body, media_items=media_items)
    except Exception as e:
        app.logger.exception(f"[FORWARD] no crítico: {e}")

    resp = MessagingResponse()
    sess_id = _sess_key(form) or "anon"
    sess = _sess_get(sess_id) or {"node_id": _flow_start_id(), "vars": {}}

    # Reinicio rápido del flujo
    if body_lc in {"reiniciar", "reset", "start", "hola", "buenas", "buenos dias", "buenas tardes", "buenas noches"}:
        sess = {"node_id": _flow_start_id(), "vars": {}}
        _flow_emit_until_input(resp, sess)
        _sess_set(sess_id, sess)
        return str(resp), 200, {"Content-Type": "application/xml"}

    # Si no hay flujo cargado
    if not _FLOW or not sess.get("node_id"):
        resp.message("🤖 Endpoint activo. Usa /generate (POST JSON) para cotizar por REST.")
        return str(resp), 200, {"Content-Type": "application/xml"}

    current_id = sess.get("node_id")
    node = _FLOW.get(current_id)

    # Nodo inválido → reinicia
    if not node:
        sess = {"node_id": _flow_start_id(), "vars": {}}
        _flow_emit_until_input(resp, sess)
        _sess_set(sess_id, sess)
        return str(resp), 200, {"Content-Type": "application/xml"}

    vars_ = sess.get("vars", {})

    # -------------------- PROCESA EL NODO --------------------
    if node.get("type") == "pregunta":
        varname = (node.get("variableName") or "").strip()
        if varname:
            vars_[varname] = body
        sess["vars"] = vars_
        sess["node_id"] = node.get("nextId")

    elif node.get("type") == "condicional":
        chosen = _try_pick_option(node, body)
        if not chosen:
            _send_menu(resp, node)
            _sess_set(sess_id, sess)
            return str(resp), 200, {"Content-Type": "application/xml"}

        save_as = chosen.get("saveAs")
        if save_as:
            vars_[save_as] = chosen.get("text", "")

        # -------- Mensajes “desde” (primer tramo del servicio + subárea) --------
        try:
            current_node_id = str(node.get("id") or "")
            sel_text = (chosen.get("text") or "").lower()

            # Cuando el nodo es selección de tipo de plaga (usa tus IDs actuales)
            if current_node_id == "1748910215188":
                subserv_label = vars_.get("subservicio", "")
                # no mostramos aún, esperamos elección de subárea

            # Nodos donde eliges Interior/Exterior/Ambas
            if current_node_id in {"1748911338220", "1748912010712", "1748912322554"}:
                subserv_label = vars_.get("subservicio", "")  # Desratización / Desinsectación
                area_sel = "interior y exterior"
                if "interior" in sel_text: area_sel = "interior"
                elif "exterior" in sel_text: area_sel = "exterior"
                elif "ambas" in sel_text or "completo" in sel_text: area_sel = "interior y exterior"

                clave = servicio_clave_plaga(subserv_label, area_sel)
                val_desde = precio_desde_prim_tramo(clave)
                if val_desde:
                    etq = area_sel.title()
                    resp.message(
                        f"{etq} desde {_fmt_money_clp(val_desde)}.\n"
                        "El valor final se ajusta según m² y complejidad.\n"
                        "Normativa: DS 594 - SEREMI - Informe sanitario."
                    )
        except Exception as e2:
            app.logger.warning(f"[mk_desde] {e2}")

        sess["vars"] = vars_
        sess["node_id"] = chosen.get("nextId") or node.get("nextId")

    else:
        sess["node_id"] = node.get("nextId")

    # -------------------- CONTINUACIÓN DEL FLUJO --------------------
    _flow_emit_until_input(resp, sess)

    if not sess.get("node_id"):
        _flow_finish_and_generate(resp, form, sess)

    _sess_set(sess_id, sess)
    return str(resp), 200, {"Content-Type": "application/xml"}

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
        "templates_dirs": TEMPLATE_DIRS,
        "out_dir": odir,
        "templates_listing_main": t_listing,
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

# -----------------------------------------------------------------------------
@app.post("/reload-flow")
def reload_flow():
    try:
        return jsonify(ok=True, count=0), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

def _log_url_map():
    try:
        logging.info("URL MAP:\n%s", app.url_map)
    except Exception:
        pass

_log_url_map()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True, use_reloader=False)
