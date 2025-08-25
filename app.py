# -*- coding: utf-8 -*-
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import json
import logging
import os
import re
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# --- Cargar flujo ---
with open('chatbot-flujo.json', 'r', encoding='utf-8') as f:
    flujo = json.load(f)

sesiones = {}  # { sender: {"current_id": str, "data": dict} }

# --- Rutas de verificación (health) ---
@app.route("/", methods=["GET"])
def index():
    return "ok", 200

@app.route("/health", methods=["GET"])
@app.route("/salud", methods=["GET"])
def health():
    return "ok", 200


# =========================
# Utilidades de flujo
# =========================
def obtener_bloque_por_id(bloque_id):
    for bloque in flujo:
        if str(bloque.get("id")) == str(bloque_id):
            logging.info(f"[FLOW] Bloque {bloque_id} -> type={bloque.get('type')}")
            return bloque
    logging.warning(f"[FLOW] Bloque {bloque_id} no encontrado")
    return None


def reemplazar_variables(texto, datos):
    texto = str(texto or "")
    for clave, valor in (datos or {}).items():
        texto = texto.replace(f"{{{clave}}}", str(valor or ""))
    return texto


def avanzar_mensajes_automaticos(sender, bloque_actual, respuesta_twilio):
    """
    Envía en cadena todos los bloques tipo 'mensaje' y devuelve el siguiente
    bloque que requiera interacción ('pregunta' o 'condicional').
    """
    while bloque_actual and bloque_actual.get("type") == "mensaje":
        contenido = reemplazar_variables(bloque_actual.get("content", ""), sesiones[sender]["data"])
        logging.info(f"[AUTO] -> {contenido}")
        respuesta_twilio.message(contenido)

        siguiente_id = bloque_actual.get("nextId")
        logging.info(f"[FLOW] nextId -> {siguiente_id}")
        if not siguiente_id:
            sesiones[sender]["current_id"] = None
            return None

        bloque_actual = obtener_bloque_por_id(siguiente_id)
        sesiones[sender]["current_id"] = str(bloque_actual["id"]) if bloque_actual else None

    return bloque_actual


# =========================
# Deducción de variables (sin depender de IDs)
# =========================
def deducir_saveas_por_opciones_y_contenido(bloque):
    """
    Determina el nombre de variable para guardar una opción de un bloque 'condicional'
    inspeccionando el 'content' y los textos de las 'options'.
    """
    content = (bloque.get("content") or "").lower()
    texts = [str(op.get("text", "")).lower() for op in bloque.get("options", [])]

    # Servicio principal
    if any("control de plagas" in t or "piscina" in t or "cámara" in t or "camaras" in t for t in texts):
        return "servicio"

    # Subservicios (piscinas)
    if any("tratamiento" in t or "mantención" in t or "mantenimiento" in t or "cambio arena" in t for t in texts):
        return "subservicio"

    # Subárea: interior / exterior / ambas
    if any("interior" in t or "exterior" in t or "ambas" in t for t in texts):
        return "subarea"

    # Rango m2
    if any("m2" in t or "metros" in t for t in texts) or ("metros" in content and "cuadrados" in content):
        return "rango_m2"

    # Material piscina
    if any("baldosa" in t or "concreto" in t or "fibra" in t for t in texts) or ("piscina" in content and "material" in content):
        return "tipo_material"

    # Cantidad de cámaras
    if any(re.search(r"\d+\s*-\s*\d+", t) for t in texts) or "número de cámaras" in content or "numero de camaras" in content:
        return "cantidad_camara"

    # Tipo de cámara
    if any("alámbricas" in t or "alambricas" in t or "inalámbricas" in t or "inalambricas" in t or "dvr" in t or "solares" in t for t in texts) \
       or ("tipo de cámara" in content or "tipo de camara" in content):
        return "tipo_camara"

    # Sí/No → acceso remoto o internet
    if any("sí" in t or "si" in t or "no" in t for t in texts):
        if "acceso remoto" in content:
            return "acceso_remoto"
        if "internet" in content:
            return "conexion_internet"

    return None


def deducir_variable_pregunta(bloque):
    """Para preguntas sin variableName explícito."""
    var = bloque.get("variableName")
    if var:
        return var
    content = (bloque.get("content") or "").lower()
    if ("áreas" in content or "areas" in content) and "vigilar" in content:
        return "area_vigilar"
    return None


# =========================
# Resumen (normaliza claves)
# =========================
def _get(d, *keys):
    for k in keys:
        v = d.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _si_no(v):
    s = str(v or "").strip().lower()
    return "Sí" if s in ("si", "sí", "yes", "true", "1") else ("No" if s in ("no", "false", "0") else s)


def enviar_resumen_por_whatsapp(data):
    try:
        account_sid   = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token    = os.getenv("TWILIO_AUTH_TOKEN")
        from_whatsapp = os.getenv("TWILIO_WHATSAPP_FROM")   # ej: whatsapp:+56958166055
        to_whatsapp   = os.getenv("NOTIFICACION_TELEFONO")  # ej: whatsapp:+569XXXXXXXX

        if not all([account_sid, auth_token, from_whatsapp, to_whatsapp]):
            logging.error("Faltan variables de entorno para enviar el resumen por WhatsApp.")
            return

        client = Client(account_sid, auth_token)

        # Datos normalizados
        nombre      = _get(data, "nombre", "name")
        direccion   = _get(data, "direccion", "dirección", "address")
        comuna      = _get(data, "comuna", "ciudad", "localidad")
        telefono    = _get(data, "telefono", "teléfono", "phone")
        email       = _get(data, "email", "correo", "mail")

        tipo_cli    = _get(data, "tipo_cliente")
        servicio    = _get(data, "servicio")
        subserv     = _get(data, "subservicio")
        subarea     = _get(data, "subarea")
        metros      = _get(data, "rango_m2", "metros_cuadrados", "m2", "cantidad_metros cuadrados")

        tam_piscina = _get(data, "tamano_piscina", "tamaño_piscina")
        mat_piscina = _get(data, "tipo_material")

        cant_cam    = _get(data, "cantidad_camara", "cantidad_cámaras")
        tipo_camara = _get(data, "tipo_camara")

        area_vig    = _get(data, "area_vigilar", "área a vigilar", "areas_a_vigilar")
        acceso_rem  = _si_no(_get(data, "acceso_remoto"))
        tiene_net   = _si_no(_get(data, "conexion_internet"))
        detalles    = _get(data, "detalles", "observaciones", "comentarios") or "No hay detalles adicionales"

        mensaje = f"""🟢 NUEVO CLIENTE SMART PLAGAS

📛 Nombre: {nombre}
🏠 Dirección: {direccion}
🏙️ Comuna: {comuna}
📞 Teléfono: {telefono}
✉️ Email: {email}
🏷️ Tipo de cliente: {tipo_cli or 'No indicado'}
🛠️ Servicio solicitado: {servicio or 'No indicado'}
🔧 Subservicio: {subserv or 'No indicado'}
📍 Área requerida: {subarea}
📐 Metros cuadrados: {metros}
🏊 Tamaño piscina: {tam_piscina}
🔩 Material piscina: {mat_piscina}
🎥 Cantidad cámaras: {cant_cam}
📷 Tipo de cámara: {tipo_camara}
📡 Área a vigilar: {area_vig}
📲 Acceso remoto: {acceso_rem}
🌐 Conexión a internet: {tiene_net}
📝 Observaciones: {detalles}
"""
        logging.info(f"[RESUMEN] {data}")
        client.messages.create(body=mensaje, from_=from_whatsapp, to=to_whatsapp)

    except Exception:
        logging.exception("❌ Error enviando resumen WhatsApp")


# =========================
# Webhook principal
# =========================
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        sender = request.form.get('From')            # whatsapp:+569...
        msg    = (request.form.get('Body') or '').strip()
        logging.info(f"[IN] {sender}: {msg}")

        respuesta = MessagingResponse()

        # Nueva sesión
        if sender not in sesiones:
            sesiones[sender] = {"current_id": str(flujo[0]["id"]), "data": {}}
            logging.info("[SESSION] Nueva sesión iniciada")

            bloque_actual = obtener_bloque_por_id(sesiones[sender]["current_id"])
            if not bloque_actual:
                respuesta.message("No encuentro el flujo inicial. Intenta más tarde.")
                return str(respuesta), 200

            bloque_actual = avanzar_mensajes_automaticos(sender, bloque_actual, respuesta)

            if bloque_actual:
                tipo = bloque_actual.get("type")
                if tipo == "pregunta":
                    contenido = reemplazar_variables(bloque_actual.get("content", ""), sesiones[sender]["data"])
                    respuesta.message(contenido)
                elif tipo == "condicional":
                    opciones = "\n".join([f"{i+1}. {op['text']}" for i, op in enumerate(bloque_actual.get("options", []))])
                    respuesta.message(f"{bloque_actual.get('content', '')}\n{opciones}")

            return str(respuesta), 200

        # Continuación de flujo
        bloque_actual = obtener_bloque_por_id(sesiones[sender]["current_id"])
        if not bloque_actual:
            respuesta.message("He reiniciado tu sesión. Escribe 'hola' para comenzar.")
            sesiones.pop(sender, None)
            return str(respuesta), 200

        tipo = bloque_actual.get("type")

        if tipo == "pregunta":
            var = deducir_variable_pregunta(bloque_actual)
            if var:
                sesiones[sender]["data"][var] = msg
                logging.info(f"[DATA] {var} = {msg}")
            siguiente_id = bloque_actual.get("nextId")
            logging.info(f"[FLOW] nextId -> {siguiente_id}")
            bloque_actual = obtener_bloque_por_id(siguiente_id)
            sesiones[sender]["current_id"] = str(bloque_actual["id"]) if bloque_actual else None

        elif tipo == "condicional":
            msg_norm = (msg or "").strip().lower()
            seleccion = None

            # 1) ¿Empieza con número? (1, 1), 1.-, etc.)
            m = re.match(r'^\s*(\d+)', msg_norm)
            if m:
                idx = int(m.group(1))
                opciones_list = bloque_actual.get("options", [])
                if 1 <= idx <= len(opciones_list):
                    seleccion = opciones_list[idx - 1]

            # 2) Si no, por texto (contiene o coincide)
            if not seleccion:
                for op in bloque_actual.get("options", []):
                    txt = op.get("text", "").lower()
                    if msg_norm == txt or msg_norm in txt or txt in msg_norm:
                        seleccion = op
                        break

            if not seleccion:
                opciones = "\n".join([f"{i+1}. {op['text']}" for i, op in enumerate(bloque_actual.get("options", []))])
                respuesta.message(f"Opción inválida. Elige una:\n{opciones}")
                return str(respuesta), 200

            # 3) Guardado: saveAs del JSON o deducción por contenido/opciones
            save_as = seleccion.get("saveAs") or deducir_saveas_por_opciones_y_contenido(bloque_actual)
            if save_as:
                sesiones[sender]["data"][save_as] = seleccion.get("text", "")
                logging.info(f"[DATA] {save_as} = {seleccion.get('text','')}")

            siguiente_id = seleccion.get("nextId")
            logging.info(f"[FLOW] nextId -> {siguiente_id}")
            bloque_actual = obtener_bloque_por_id(siguiente_id)
            sesiones[sender]["current_id"] = str(bloque_actual["id"]) if bloque_actual else None

        # Avanza mensajes automáticos y siguiente interacción
        bloque_actual = avanzar_mensajes_automaticos(sender, bloque_actual, respuesta)

        if bloque_actual:
            tipo = bloque_actual.get("type")
            if tipo == "pregunta":
                contenido = reemplazar_variables(bloque_actual.get("content", ""), sesiones[sender]["data"])
                respuesta.message(contenido)
            elif tipo == "condicional":
                opciones = "\n".join([f"{i+1}. {op['text']}" for i, op in enumerate(bloque_actual.get("options", []))])
                respuesta.message(f"{bloque_actual.get('content', '')}\n{opciones}")
        else:
            # Flujo terminó
            enviar_resumen_por_whatsapp(sesiones[sender]["data"])
            respuesta.message("✅ ¡Gracias! Recibimos tu solicitud. En breve te enviaremos tu cotización.")
            logging.info(f"[SESSION END] Data: {sesiones[sender]['data']}")
            sesiones.pop(sender, None)

        return str(respuesta), 200

    except Exception:
        logging.exception("❌ Error inesperado:")
        respuesta = MessagingResponse()
        respuesta.message("Ha ocurrido un error. Intenta nuevamente.")
        return str(respuesta), 200


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)