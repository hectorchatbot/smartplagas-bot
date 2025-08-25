# -*- coding: utf-8 -*-
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import json
import logging
import os
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

# --- Utilidades ---
def obtener_bloque_por_id(bloque_id):
    for bloque in flujo:
        if str(bloque.get("id")) == str(bloque_id):
            return bloque
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
        logging.info(f"[AUTO] Enviando mensaje: {contenido}")
        respuesta_twilio.message(contenido)

        siguiente_id = bloque_actual.get("nextId")
        if not siguiente_id:
            sesiones[sender]["current_id"] = None
            return None

        bloque_actual = obtener_bloque_por_id(siguiente_id)
        sesiones[sender]["current_id"] = str(bloque_actual["id"]) if bloque_actual else None

    return bloque_actual

def enviar_resumen_por_whatsapp(data_cliente):
    try:
        account_sid   = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token    = os.getenv("TWILIO_AUTH_TOKEN")
        from_whatsapp = os.getenv("TWILIO_WHATSAPP_FROM")   # ej: whatsapp:+56958166055
        to_whatsapp   = os.getenv("NOTIFICACION_TELEFONO")  # ej: whatsapp:+569XXXXXXXX

        if not all([account_sid, auth_token, from_whatsapp, to_whatsapp]):
            logging.error("Faltan variables de entorno para enviar el resumen por WhatsApp.")
            return

        client = Client(account_sid, auth_token)

        mensaje = f"""🟢 NUEVO CLIENTE SMART PLAGAS

📛 Nombre: {data_cliente.get('nombre', '')}
🏠 Dirección: {data_cliente.get('direccion', '')}
🏙️ Comuna: {data_cliente.get('comuna', '')}
📞 Teléfono: {data_cliente.get('telefono', '')}
✉️ Email: {data_cliente.get('email', '')}
🏷️ Tipo de cliente: {data_cliente.get('tipo_cliente', 'No indicado')}
🛠️ Servicio solicitado: {data_cliente.get('servicio', 'No indicado')}
🔧 Subservicio: {data_cliente.get('subservicio', 'No indicado')}
📍 Área requerida: {data_cliente.get('subarea', '')}
📐 Metros cuadrados: {data_cliente.get('cantidad_metros cuadrados', '')}
🏊 Tamaño piscina: {data_cliente.get('tamano_piscina', '')}
🔩 Material piscina: {data_cliente.get('tipo_material', '')}
🎥 Cantidad cámaras: {data_cliente.get('cantidad_camara', '')}
📷 Tipo de cámara: {data_cliente.get('tipo_camara', '')}
📡 Área a vigilar: {data_cliente.get('¿Qué áreas deseas vigilar?', '')}
📲 Acceso remoto: {data_cliente.get('¿Podrías tener acceso remoto desde celular o PC una vez instalado el sistema?', '')}
🌐 Conexión a internet: {data_cliente.get('¿Cuenta con conexión a internet en el lugar de instalación?', '')}
📝 Observaciones: {data_cliente.get('detalles', 'No hay detalles adicionales')}
"""
        client.messages.create(body=mensaje, from_=from_whatsapp, to=to_whatsapp)

    except Exception as e:
        logging.error(f"❌ Error al enviar resumen por WhatsApp: {e}")

# --- Webhook principal ---
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        sender = request.form.get('From')            # whatsapp:+569...
        msg    = (request.form.get('Body') or '').strip()
        logging.info(f"Mensaje de {sender}: {msg}")

        respuesta = MessagingResponse()

        # Nueva sesión
        if sender not in sesiones:
            sesiones[sender] = {"current_id": str(flujo[0]["id"]), "data": {}}
            logging.info("Nueva sesión iniciada")

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
            var = bloque_actual.get("variableName")
            if var:
                sesiones[sender]["data"][var] = msg
            siguiente_id = bloque_actual.get("nextId")
            bloque_actual = obtener_bloque_por_id(siguiente_id)
            sesiones[sender]["current_id"] = str(bloque_actual["id"]) if bloque_actual else None

        elif tipo == "condicional":
            msg_normalizado = msg.lower()
            seleccion = None
            for i, opcion in enumerate(bloque_actual.get("options", []), 1):
                texto_opcion = opcion["text"].lower()
                if msg_normalizado == str(i) or msg_normalizado in texto_opcion:
                    seleccion = opcion
                    break
            if seleccion:
                if "saveAs" in seleccion:
                    sesiones[sender]["data"][seleccion["saveAs"]] = seleccion["text"]
                siguiente_id = seleccion.get("nextId")
                bloque_actual = obtener_bloque_por_id(siguiente_id)
                sesiones[sender]["current_id"] = str(bloque_actual["id"]) if bloque_actual else None
            else:
                opciones = "\n".join([f"{i+1}. {op['text']}" for i, op in enumerate(bloque_actual.get("options", []))])
                respuesta.message(f"Opción inválida. Elige una de estas:\n{opciones}")
                return str(respuesta), 200

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
            sesiones.pop(sender, None)

        return str(respuesta), 200

    except Exception as e:
        logging.exception("❌ Error inesperado:")
        respuesta = MessagingResponse()
        respuesta.message("Ha ocurrido un error. Intenta nuevamente.")
        return str(respuesta), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)
