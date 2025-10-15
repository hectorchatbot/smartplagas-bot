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
# Config bÃ¡sica / logging
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
load_dotenv(override=False)
APP_VERSION = os.getenv("RAILWAY_GIT_COMMIT_SHA", "dev-local")

# Profundidad por defecto para piscinas (si el usuario no la entrega)
POOL_DEFAULT_DEPTH = float(os.getenv("POOL_DEFAULT_DEPTH", "1.4"))
# Volumen mÃ­nimo asumido en piscinas cuando no hay m2/m3
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
    FILES_DIR,  # por si se subiÃ³ vÃ­a /upload
]

# EnvÃ­os
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
    "desinsectacion":[56000,71250,97500,120000,157500,247500,405000,660000],
    "desratizacion": [51000,66000,90000,112500,146250,225000,352500,562500],
    "desinfeccion":  [42000,56000,77000,98000,126000,196000,308000,490000],
}
TRAMOS_M3 = [(0,25),(26,50),(51,100),(101,999999)]
PRECIOS_PISCINA = {
    "piscina_plan_intermedio_m3":  [4400,3800,3500,3250],
    "piscina_mantencion_bomba_m3": [3600,3350,3150,2900],
    "piscina_shock_m3":            [1700,1450,1250,1000],
    "piscina_diagnostico_total":   [34000,39000,45000,50500],
    "piscina_cambio_arena_total":  [101000,157000,224000,336000],
}
CAM_PRECIOS = {
    "alambricas":   {"interior":77000,"exterior":99000},
    "inalambricas": {"interior":66000,"exterior":88000},
    "solares":      {"exterior":165000},
    "dvr":          {"interior":82500,"exterior":104500},
}

# =====================[ FACTORES Y HELPERS PLAGAS ]=====================
FACTOR_INTERIOR = 0.6
FACTOR_EXTERIOR = 0.4
FACTOR_AMBAS    = 1.0

def _tipo_servicio(vars_: dict) -> str:
    """
    Retorna 'desinsectacion', 'desratizacion' o '' detectando desde campos comunes.
    Acepta claves: servicio_label, servicio_precio, servicio, tipo_servicio, categoria, service, tipo.
    """
    keys = ["servicio_precio", "servicio_label", "servicio", "tipo_servicio", "categoria", "service", "tipo"]
    txt = ""
    for k in keys:
        txt = (vars_.get(k, "") or "").strip().lower()
        if txt:
            break
    if any(w in txt for w in ["desinsect", "insect"]):
        return "desinsectacion"
    if any(w in txt for w in ["desrat", "roedor", "rat"]):
        return "desratizacion"
    return ""

def _scope_from_vars(vars_: dict) -> str:
    """
    Devuelve 'interior', 'exterior' o 'ambas' segÃºn variables del flujo.
    Acepta interior_exterior, area_tratamiento, tratamiento_area, zona, scope, alcance, area_plaga, interiorExterior.
    """
    keys = ["interior_exterior","area_tratamiento","tratamiento_area","zona","scope","alcance","area_plaga","interiorExterior"]
    raw = ""
    for k in keys:
        raw = (vars_.get(k, "") or "").strip().lower()
        if raw:
            break
    if any(w in raw for w in ["ambas","ambos","interior y exterior","interior & exterior","i/e","todo","completo"]):
        return "ambas"
    if "interior" in raw and "exterior" in raw:
        return "ambas"
    if "interior" in raw or raw == "i":
        return "interior"
    if "exterior" in raw or raw == "e":
        return "exterior"
    return ""

def _scope_from_any(d: dict) -> str:
    """
    Fallback: intenta deducir el alcance desde cualquier campo/label visible.
    """
    s = _scope_from_vars(d)
    if s:
        return s
    lbls = [
        d.get("servicio_label",""), d.get("servicio",""),
        d.get("subservicio",""), d.get("detalle",""), d.get("detalles","")
    ]
    raw = " ".join(x for x in lbls if x).lower()
    if any(w in raw for w in ["interior y exterior","interior & exterior","ambas","ambos","completo"]):
        return "ambas"
    if "interior" in raw and "exterior" in raw:
        return "ambas"
    if "interior" in raw:
        return "interior"
    if "exterior" in raw:
        return "exterior"
    return ""

def _metros_desde_vars(vars_: dict) -> float:
    """
    Obtiene m2 desde campos comunes.
    """
    keys = ["m2","metros2","metros_cuadrados","area_m2","superficie_m2","superficie"]
    for k in keys:
        v = vars_.get(k)
        if v is None:
            continue
        try:
            num = re.sub(r"[^\d.,-]", "", str(v)).replace(",", ".")
            return max(0.0, float(num))
        except:
            continue
    return 0.0

def _factor_por_scope(scope: str) -> float:
    if scope == "interior":
        return FACTOR_INTERIOR
    if scope == "exterior":
        return FACTOR_EXTERIOR
    if scope == "ambas":
        return FACTOR_AMBAS
    return FACTOR_AMBAS  # por defecto (total)

def aplicar_factor_control_plagas(precio_tramo_base: float, tipo: str, scope: str) -> float:
    """
    Solo para 'desinsectacion' y 'desratizacion':
      interior -> 0.6
      exterior -> 0.4
      ambas    -> 1.0
    Otros servicios retornan el base sin cambios.
    """
    if tipo in ("desinsectacion","desratizacion"):
        return round(precio_tramo_base * _factor_por_scope(scope), 0)
    return precio_tramo_base

def build_descripcion_pdf(tipo: str, m2: float, scope: str) -> str:
    """
    DescripciÃ³n para PDF:
    - DesinsectaciÃ³n: ðŸœðŸ•·ï¸ DesinsectaciÃ³n â€” 150 m2 - interior|exterior|interior y exterior
    - DesratizaciÃ³n : DesratizaciÃ³n â€” 150 m2 - interior|exterior|interior y exterior
    - Otros         : Servicio de control de plagas â€” ...
    """
    m2_txt = f"{int(m2)} m2" if m2 and abs(m2 - int(m2)) < 0.01 else (f"{m2:g} m2" if m2 else "")
    if scope == "ambas":
        alcance = "interior y exterior"
    elif scope in ("interior","exterior"):
        alcance = scope
    else:
        alcance = ""

    if tipo == "desinsectacion":
        base = "ðŸœðŸ•·ï¸ DesinsectaciÃ³n"
    elif tipo == "desratizacion":
        base = "DesratizaciÃ³n"
    else:
        base = "Servicio de control de plagas"

    if m2_txt:
        base += f" â€” {m2_txt}"
    if alcance:
        base += f" - {alcance}"
    return base

def build_etiqueta_servicio_tabla(tipo: str, m2: float, scope: str) -> str:
    """
    Mismo texto que la descripciÃ³n pero SIN emojis (para la celda 'servicio' de la tabla).
    """
    m2_txt = f"{int(m2)} m2" if m2 and abs(m2 - int(m2)) < 0.01 else (f"{m2:g} m2" if m2 else "")
    if scope == "ambas":
        alcance = "interior y exterior"
    elif scope in ("interior","exterior"):
        alcance = scope
    else:
        alcance = ""

    titulo = {
        "desinsectacion": "DesinsectaciÃ³n",
        "desratizacion":  "DesratizaciÃ³n"
    }.get(tipo, "Servicio de control de plagas")

    txt = titulo
    if m2_txt:
        txt += f" â€” {m2_txt}"
    if alcance:
        txt += f" - {alcance}"
    return txt

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
    exterior_words = ("exterior","patio","jardin","jardÃ­n","porton","portÃ³n","entrada","estacionamiento","perimetro","perÃ­metro","terraza","muro")
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
    if "mas" in t or "mÃ¡s" in t or "5" in t: return 6
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

def _canon_piscina_key(label: str) -> str:
    """Mapea label humano a clave de precios de piscina."""
    s = _norm(label)
    if "plan intermedio" in s or ("tratamient" in s and "limpiez" in s): return "piscina_plan_intermedio_m3"
    if ("bomba" in s) or ("filtro" in s) or ("mantencion" in s):         return "piscina_mantencion_bomba_m3"
    if ("shock" in s) or ("clor" in s):                                   return "piscina_shock_m3"
    if ("diagn" in s):                                                    return "piscina_diagnostico_total"
    if ("arena" in s) or ("carga" in s):                                  return "piscina_cambio_arena_total"
    return ""

def _dominio_from_info(info: dict) -> str:
    """
    DetecciÃ³n robusta del dominio:
    - Piscinas si label mapea a una key de piscina o si hay campos tÃ­picos (tamaÃ±o, profundidad, m3).
    - CÃ¡maras si hay tipo/cantidad/Ã¡rea de cÃ¡maras.
    - Plagas si texto contiene plaga/desratiz/desinsect/sanitiz.
    """
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

def _canon_servicio_para_precios(servicio_humano: str) -> str:
    s = _strip_accents_and_symbols(servicio_humano)
    if "desratiz" in s:  return "desratizacion"
    if "desinfecc" in s: return "desinfeccion"
    if "desinsect" in s: return "desinsectacion"
    return "desinsectacion"

# --- Parseo "6x3", "10 x 4,5", "8x4mts" o solo "56" -> m2 ---
def parse_pool_size_to_m2(size_text: str) -> float:
    if not size_text:
        return 0.0
    s = str(size_text).lower()
    s = s.replace("metros", "").replace("metro", "")
    s = s.replace("m2", "").replace("mÂ²", "").replace("mts", "").replace("mt", "").replace("m", "")
    s = s.replace(",", ".").strip()
    s = s.replace("por", "x").replace("*", "x")
    s = re.sub(r"\s+", "", s)
    m = re.match(r"^(\d+(?:\.\d+)?)[xÃ—](\d+(?:\.\d+)?)$", s)
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

def precio_por_tramo(servicio_precio: str, m2: float) -> int:
    tabla = PRECIOS.get(servicio_precio)
    if not tabla: return 0
    m2n = int(float(m2) if m2 else 0)
    for idx, (lo, hi) in enumerate(TRAMOS):
        if lo <= m2n <= hi: return int(tabla[idx])
    return int(tabla[-1])

def _volumen_estimado_m3(info: dict) -> float:
    # 1) explÃ­cito
    for k in ("m3","volumen","volumen_m3"):
        v = str(info.get(k, "") or "").strip()
        if v:
            try: return float(v.replace(",", "."))
            except Exception: pass
    # 2) m2 * profundidad (si no hay profundidad, usa default)
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
    # Si el servicio es por m3 y no hay m3, aplicar m3 mÃ­nimo (fallback definitivo)
    if serv_key.endswith("_m3") and (m3 is None or m3 <= 0):
        m3 = PISCINA_MIN_M3_DEFAULT
    # seleccionar tramo
    idx = len(TRAMOS_M3) - 1
    for i, (lo, hi) in enumerate(TRAMOS_M3):
        if lo <= m3 <= hi: idx = i; break
    if serv_key.endswith("_m3"):
        unit = tabla[idx]
        return int(round(unit * m3))
    # tarifa fija por total
    return int(tabla[idx] or 0)

def precio_total(info: dict) -> int:
    dominio = _dominio_from_info(info)

    if dominio == "piscinas":
        label = info.get("servicio_label", "")
        override = (info.get("servicio_precio") or "").strip()
        key = override if override in PRECIOS_PISCINA else (_canon_piscina_key(label) or "piscina_plan_intermedio_m3")
        m3 = _volumen_estimado_m3(info)
        # Fallback para TODOS los servicios por m3
        if key.endswith("_m3") and (m3 is None or m3 <= 0):
            m3 = PISCINA_MIN_M3_DEFAULT
            info["__m3_asumido__"] = True
            info["__m3_asumido_val__"] = m3
        return _precio_piscina_por_tramo(key, m3)

    elif dominio == "plagas":
        # SOLO plagas: aplicar factor 0.6/0.4/1.0 a desinsectación/desratización
        tipo  = _tipo_servicio(info)               # 'desinsectacion' | 'desratizacion' | ''
        scope = _scope_from_any(info)              # 'interior' | 'exterior' | 'ambas' | ''
        base  = precio_por_tramo(info.get("servicio_precio",""), info.get("m2") or 0)
        total = aplicar_factor_control_plagas(base, tipo, scope)
        return int(total)

    elif dominio == "camaras":
        total, _, _, _, _ = calcular_total_camaras(
            info.get("tipo_camara",""),
            info.get("area_vigilar",""),
            info.get("cantidad_camara","")
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

# --- Marketing "desde" (interior/exterior/completo) con split 60/40 ---
SPLIT_IE = {"interior": 0.60, "exterior": 0.40}

def precios_desde_por_servicio(serv_key: str) -> dict:
    """Retorna 'desde' usando el primer tramo (0-50 mÂ²) y split 60/40."""
    tabla = PRECIOS.get(serv_key)
    if not tabla:
        return {}
    base = int(tabla[0])  # tramo 0-50 mÂ² como "desde"
    return {
        "completo": base,
        "interior": int(round(base * SPLIT_IE["interior"])),
        "exterior": int(round(base * SPLIT_IE["exterior"])),
    }

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
        raise RuntimeError("LibreOffice no estÃ¡ disponible en el contenedor.")
    cmd = [bin_lo, "--headless", "--convert-to", "pdf", "--outdir", outdir, docx_path]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    base_pdf = os.path.splitext(os.path.basename(docx_path))[0] + ".pdf"
    generated = os.path.join(outdir, base_pdf)
    if os.path.exists(generated) and generated != pdf_path:
        os.replace(generated, pdf_path)
    if not os.path.exists(pdf_path):
        raise RuntimeError("LibreOffice no generÃ³ el PDF")

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
# SelecciÃ³n de plantilla + Render DOCX
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
            m2_val = _metros_desde_vars(info)
            m2_txt = str(int(m2_val)) if (m2_val and float(m2_val).is_integer()) else (str(m2_val) if m2_val else "")

        tipo = _tipo_servicio(info)
        scope = _scope_from_any(info)

        desc_pdf = build_descripcion_pdf(tipo, m2_val, scope)
        etiqueta = build_etiqueta_servicio_tabla(tipo, m2_val, scope)

        total_int = precio_total(info)

        ctx["m2"] = m2_txt
        ctx["linea_servicio"] = etiqueta
        ctx["linea_medida"] = m2_txt if m2_txt else "1"
        ctx["linea_total"] = _fmt_money_clp(total_int)
        ctx["precio"] = _fmt_money_clp(total_int)
        ctx["total"] = _fmt_money_clp(total_int)
        ctx["descripcion"] = desc_pdf
        ctx["clausula_seremi"] = " — con instalación de estaciones cebaderas y entrega de informe sanitario conforme a exigencias SEREMI." if tipo == "desratizacion" else ""

    elif dom == "piscinas":
        try:
            m2_val = float(info.get("m2") or 0)
        except Exception:
            m2_val = 0.0

        label = info.get("servicio_label", "")
        key = _canon_piscina_key(label) or "piscina_plan_intermedio_m3"
        m3_val = _volumen_estimado_m3(info)
        if key.endswith("_m3") and (m3_val is None or m3_val <= 0):
            m3_val = info.get("__m3_asumido_val__", PISCINA_MIN_M3_DEFAULT)
            info["__m3_asumido__"] = True
            info["__m3_asumido_val__"] = m3_val

        total_int = _precio_piscina_por_tramo(key, m3_val)

        ctx["precio"] = _fmt_money_clp(total_int)
        ctx["total"] = _fmt_money_clp(total_int)
        ctx["linea_total"] = _fmt_money_clp(total_int)

        m3_txt = str(int(m3_val)) if (m3_val and float(m3_val).is_integer()) else (str(m3_val) if m3_val else "")
        m2_txt = str(int(m2_val)) if (m2_val and float(m2_val).is_integer()) else (str(m2_val) if m2_val else "")
        ctx["m2"] = m2_txt
        ctx["m3"] = m3_txt
        ctx["linea_servicio"] = info["servicio_label"]
        ctx["linea_medida"] = m3_txt if m3_txt else (m2_txt if m2_txt else "1")
        ctx["descripcion"] = info["servicio_label"]
        ctx["clausula_seremi"] = ""

    else:
        ctx["descripcion"] = info["servicio_label"]

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
        sids["admin_text"] = send_whatsapp_text(ADMIN_WA, "ðŸ§¾ *Nueva cotizaciÃ³n*\n\n" + resumen_texto, delay=0.0)
    if pdf_url:
        sids["admin_pdf"]  = send_whatsapp_media_only_pdf(ADMIN_WA, "ðŸ“„ PDF de la cotizaciÃ³n", pdf_url, delay=MEDIA_DELAY)
    if docx_url:
        sids["admin_docx"] = send_whatsapp_text(ADMIN_WA, f"ðŸ–¹ DOCX: {docx_url}", delay=MEDIA_DELAY)
    return sids

# -----------------------------------------------------------------------------
# NormalizaciÃ³n de payload externo y generate
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
    tamano_piscina = _safe(data.get("tamano_piscina") or data.get("tamaÃ±o_piscina"))
    m3_explicit    = _safe(data.get("m3") or data.get("volumen") or data.get("volumen_m3"))

    # cÃ¡maras:
    tipo_camara     = _safe(data.get("tipo_camara"))
    cantidad_camara = _safe(data.get("cantidad_camara"))
    area_vigilar    = _safe(data.get("area_vigilar"))

    # m2 declarado
    try:
        m2_num = float((m2_raw or "0").lower().replace("m2","").replace("mÂ²","").replace(",",".").strip() or "0")
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

    servicio_label  = servicio or "DesinsectaciÃ³n"
    servicio_precio = _canon_servicio_para_precios(servicio_label)

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
        # cÃ¡maras:
        "tipo_camara": tipo_camara,
        "cantidad_camara": cantidad_camara,
        "area_vigilar": area_vigilar,
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

    # Si es piscina detectada por estructura/texto, asegurar prefijo claro
    if _dominio_from_info(info) == "piscinas" and "piscin" not in _norm(info.get("servicio_label","")):
        info["servicio_label"] = f"Piscinas â€“ {info.get('servicio_label','')}"

    faltantes = [k for k in ("servicio_label","cliente","direccion","contacto") if not info.get(k)]
    if faltantes:
        return jsonify(ok=True, message="Campos mÃ­nimos faltantes; no se generan archivos",
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
            medidas_line = f"*Volumen (asumido):* {vol} mÂ³\n"
        else:
            medidas_line = f"*Volumen:* {vol} mÂ³\n"
    elif dominio == "plagas":
        medidas_line = f"*Superficie tratada:* {info.get('m2',0)} mÂ²\n"
    elif dominio == "camaras":
        tot, tipo, qty, unit_ap, area = calcular_total_camaras(
            info.get("tipo_camara",""), info.get("area_vigilar",""), info.get("cantidad_camara","")
        )
        detalle_line = f"*CÃ¡maras:* {info.get('tipo_camara','')} ({area}) x {qty} â€” unit: {_fmt_money_clp(unit_ap)}\n"

    partes = [
        "âœ… *Nueva solicitud recibida*\n",
        f"*Servicio:* {info['servicio_label']}\n",
        detalle_line,
        f"*Cliente:* {info['cliente']}\n",
        medidas_line,
        f"*UbicaciÃ³n:* {info['direccion']}\n",
    ]
    if info.get("comuna"): partes.append(f"*Comuna:* {info['comuna']}\n")
    partes.extend([f"*Detalles:* {info.get('detalles','')}\n",
                   f"*Contacto:* {info['contacto']} | {info['email']}\n", f"*Total:* {total}"])
    resumen = "".join(partes)

    sids = {}
    if info.get("to_whatsapp") and SEND_PDF:
        sids["client_pdf"] = send_whatsapp_media_only_pdf(info["to_whatsapp"], "ðŸ“„ CotizaciÃ³n adjunta", pdf_url, MEDIA_DELAY)
        if SEND_DOC:
            send_whatsapp_text(info["to_whatsapp"], f"ðŸ–¹ DOCX: {docx_url}", delay=MEDIA_DELAY)

    if SEND_COPY_TO_ADMIN and ADMIN_WA:
        sids["admin"] = send_admin_copy(resumen, pdf_url, docx_url)

    dbg = {
        "dominio": dominio,
        "precio_key_piscina": (info.get("servicio_precio") if info.get("servicio_precio") in PRECIOS_PISCINA else _canon_piscina_key(info.get("servicio_label",""))),
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
            "tamano_piscina": "tamano_piscina", "tamaÃ±o_piscina": "tamano_piscina",
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
        "servicio":  "Â¿QuÃ© servicio necesitas? (ej: *Piscinas - Plan Intermedio*, *DesratizaciÃ³n*, *CÃ¡maras - InalÃ¡mbricas*)",
        "m2":        "Â¿CuÃ¡l es la *superficie en mÂ²*? (ej: 56)",
        "profundidad":"Para piscinas, Â¿profundidad en *metros*? (ej: 1.4). Escribe *omitir* si no aplica.",
        "direccion": "Â¿DirecciÃ³n exacta?",
        "comuna":    "Â¿Comuna?",
        "contacto":  "Â¿Nombre de contacto?",
        "email":     "Â¿Email de contacto?",
        "telefono":  "Â¿TelÃ©fono (con cÃ³digo paÃ­s, ej: +569xxxxxxxx)?",
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
    if "200" in s or "mÃ¡s" in s or ">" in s: return 250
    return 0

def _compose_payload_from_vars(vars_, from_wa: str):
    servicio = vars_.get("servicio", "")
    subservicio = vars_.get("subservicio", "")
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
        servicio_label = f"Piscinas â€“ {subservicio or 'Servicio'}"
    elif "cÃ¡mara" in (servicio or "").lower() or "camara" in (servicio or "").lower():
        servicio_label = f"CÃ¡maras â€“ {subservicio or 'Servicio'}"
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
    }

    if not payload.get("phone") and from_wa:
        payload["phone"] = from_wa.replace("whatsapp:", "")

    return payload

def _flow_finish_and_generate(resp, form, sess):
    vars_ = sess.get("vars", {})
    from_wa = form.get("From") or ""
    payload = _compose_payload_from_vars(vars_, from_wa)

    info = normalize_payload(payload)

    # Seguridad extra: prefijo â€œPiscinas â€“ â€¦â€ si aplica
    if _dominio_from_info(info) == "piscinas" and "piscin" not in _norm(info.get("servicio_label","")):
        info["servicio_label"] = f"Piscinas â€“ {info.get('servicio_label','')}"

    if (docx2pdf_convert is None) and (not _lo_bin()):
        resp.message("âš ï¸ No pude generar PDF por un problema interno de conversiÃ³n. Intentaremos de nuevo pronto.")
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
        resp.message("âš ï¸ No pude generar la cotizaciÃ³n. Intenta nuevamente.")
        return

    docx_url, pdf_url = build_urls(docx_name, pdf_name)
    total_int = precio_total(info)
    total = _fmt_money_clp(total_int)

    resumen = (
        "âœ… CotizaciÃ³n lista\n"
        f"â€¢ Servicio: {info.get('servicio_label','')}\n"
        f"â€¢ Total: {total}\n"
        f"â€¢ PDF: {pdf_url}"
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

    resp = MessagingResponse()
    sess_id = _sess_key(form) or "anon"
    sess = _sess_get(sess_id) or {"node_id": _flow_start_id(), "vars": {}}

    # Reinicio rÃ¡pido del flujo
    if body_lc in {"reiniciar", "reset", "start", "hola", "buenas", "buenos dias", "buenas tardes", "buenas noches"}:
        sess = {"node_id": _flow_start_id(), "vars": {}}
        _flow_emit_until_input(resp, sess)
        _sess_set(sess_id, sess)
        return str(resp), 200, {"Content-Type": "application/xml"}

    # Si no hay flujo cargado
    if not _FLOW or not sess.get("node_id"):
        resp.message("ðŸ¤– Endpoint activo. Usa /generate (POST JSON) para cotizar por REST.")
        return str(resp), 200, {"Content-Type": "application/xml"}

    current_id = sess.get("node_id")
    node = _FLOW.get(current_id)

    # Nodo invÃ¡lido â†’ reinicia
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

        # Mensajes â€œdesdeâ€ automÃ¡ticos (split 60/40 interior/exterior)
        try:
            current_node_id = str(node.get("id") or "")
            sel_text = (chosen.get("text") or "").lower()

            # Tipo de plaga
            if current_node_id == "1748910215188":
                subserv_label = vars_.get("subservicio", "")
                serv_key = _canon_servicio_para_precios(subserv_label)
                mk = precios_desde_por_servicio(serv_key)
                if mk:
                    resp.message(
                        "Precios desde (referencia):\n"
                        f"- Interior: {_fmt_money_clp(mk['interior'])}\n"
                        f"- Exterior: {_fmt_money_clp(mk['exterior'])}\n"
                        f"- Completo: {_fmt_money_clp(mk['completo'])}\n"
                        "Normativa: DS 594 - SEREMI - Informe sanitario."
                    )

            # Interior/Exterior/Ambas
            if current_node_id in {"1748911338220", "1748912010712", "1748912322554"}:
                subserv_label = vars_.get("subservicio", "")
                serv_key = _canon_servicio_para_precios(subserv_label)
                mk = precios_desde_por_servicio(serv_key)
                if mk:
                    if "interior" in sel_text:
                        val = mk["interior"]; etq = "Interior"
                    elif "exterior" in sel_text:
                        val = mk["exterior"]; etq = "Exterior"
                    else:
                        val = mk["completo"]; etq = "Completo (interior + exterior)"
                    resp.message(
                        f"{etq} desde {_fmt_money_clp(val)}.\n"
                        "El valor final se ajusta segÃºn mÂ² y complejidad.\n"
                        "Normativa: DS 594 - SEREMI - Informe sanitario."
                    )
        except Exception as e2:
            app.logger.warning(f"[mk_desde] {e2}")

        sess["vars"] = vars_
        sess["node_id"] = chosen.get("nextId") or node.get("nextId")

    else:
        sess["node_id"] = node.get("nextId")

    # -------------------- CONTINUACIÃ“N DEL FLUJO --------------------
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


@app.route("/tramos", methods=["GET"])
def tramos_html():
    """
    Tabla HTML de TRAMOS + PRECIOS por servicio.
    ParÃ¡metros opcionales:
      - servicio: desinsectacion | desratizacion | desinfeccion
      - scope: interior | exterior | ambas (factor solo para desinsectaciÃ³n/desratizaciÃ³n)
    """
    servicio = (request.args.get("servicio") or "").strip().lower()
    scope    = (request.args.get("scope") or "").strip().lower()

    servicios_validos = list(PRECIOS.keys())
    if not servicio or servicio not in PRECIOS:
        servicio = "desinsectacion"

    precios = PRECIOS[servicio]
    filas = []
    for i, (lo, hi) in enumerate(TRAMOS):
        base = int(precios[i])
        total = base
        if servicio in ("desinsectacion","desratizacion") and scope in ("interior","exterior","ambas"):
            total = int(aplicar_factor_control_plagas(base, servicio, scope))
        filas.append((f"{lo}-{hi} mÂ²", base, (total if scope else None)))

    # HTML simple y limpio
    css = """
    <style>
      :root { --bg:#0b1320; --card:#111a2b; --txt:#e6edf3; --mut:#9fb3c8; --acc:#4ea1ff; }
      body{background:var(--bg);font-family:system-ui,Segoe UI,Roboto,Arial;color:var(--txt);margin:0;padding:24px;}
      .card{background:var(--card);border-radius:16px;padding:20px;max-width:980px;margin:0 auto;box-shadow:0 8px 24px rgba(0,0,0,.35);}
      h1{margin:0 0 12px;font-size:22px}
      .mut{color:var(--mut);margin:0 0 16px}
      .controls{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px}
      select, a.btn{background:#0e2239;border:1px solid #1e3350;color:#e6edf3;border-radius:10px;padding:8px 10px;font-size:14px;text-decoration:none}
      a.btn:hover{border-color:var(--acc)}
      table{width:100%;border-collapse:collapse;border-spacing:0}
      th,td{padding:10px 12px;border-bottom:1px solid #21324a;text-align:left;font-size:14px}
      th{color:#b8c7d9;font-weight:600}
      td.money{font-variant-numeric: tabular-nums}
      .pill{display:inline-block;padding:2px 8px;border-radius:999px;border:1px solid #234a73;background:#102844;color:#9fd0ff;font-size:12px}
      .note{margin-top:10px;color:#9fb3c8;font-size:13px}
    </style>
    """

    def fmt(n): 
        try: return f"${int(n):,}".replace(",", ".")
        except: return "$0"

    opts_serv = "".join(
        f'<option value="{s}" {"selected" if s==servicio else ""}>{s}</option>' for s in servicios_validos
    )
    scopes = ["", "interior", "exterior", "ambas"]
    labels = {"":"(sin factor)","interior":"interior (0.6)","exterior":"exterior (0.4)","ambas":"ambas (1.0)"}
    opts_scope = "".join(
        f'<option value="{s}" {"selected" if s==scope else ""}>{labels[s]}</option>' for s in scopes
    )

    rows = []
    for tramo, base, tot in filas:
        rows.append(
            f"<tr><td>{tramo}</td><td class='money'>{fmt(base)}</td><td class='money'>{fmt(tot) if tot is not None else 'â€”'}</td></tr>"
        )
    rows_html = "\n".join(rows)

    tip = ""
    if scope:
        tip = f"<span class='pill'>scope: {labels.get(scope, scope)}</span>"
    hdr = f"<h1>Tramos y precios â€” {servicio}</h1><p class='mut'>Tabla por tramo (mÂ²). {tip}</p>"

    controls = f"""
    <form class="controls" method="get" action="/tramos">
      <label>Servicio&nbsp;
        <select name="servicio">{opts_serv}</select>
      </label>
      <label>Alcance&nbsp;
        <select name="scope">{opts_scope}</select>
      </label>
      <button class="btn" type="submit">Ver</button>
      <a class="btn" href="/_debug/tabla_servicio?servicio={servicio}{('&scope='+scope) if scope else ''}" target="_blank">Ver JSON</a>
    </form>
    """

    table = f"""
    <table>
      <thead><tr><th>Tramo</th><th>Precio base</th><th>Precio con factor</th></tr></thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
    <div class="note">* El factor (interior/exterior/ambas) solo aplica a DesinsectaciÃ³n y DesratizaciÃ³n.</div>
    """

    html = f"<!doctype html><html><head><meta charset='utf-8'><title>Tramos</title>{css}</head><body><div class='card'>{hdr}{controls}{table}</div></body></html>"
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


# -----------------------------------------------------------------------------
# /generate (REST)
# -----------------------------------------------------------------------------
@app.post("/generate")
def generate(): return handle_generate()

# -----------------------------------------------------------------------------
# /upload Ãºnico (con token)
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
