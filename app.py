from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import json
import logging
import os

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Cargar flujo JSON
with open('chatbot-flujo.json', 'r', encoding='utf-8') as f:
    flujo = json.load(f)

# Sesiones en memoria (en producción usar BD si se desea persistencia)
sesiones = {}

def obtener_bloque_por_id(bloque_id):
    for bloque in flujo:
        if str(bloque["id"]) == str(bloque_id):
            return bloque
    return None

def reemplazar_variables(texto, datos):
    for clave, valor in datos.items():
        texto = texto.replace(f"{{{clave}}}", valor)
    return texto

def avanzar_mensajes_automaticos(sender, bloque_actual, respuesta_twilio):
    while bloque_actual and bloque_actual["type"] == "mensaje":
        contenido = reemplazar_variables(bloque_actual["content"], sesiones[sender]["data"])
        logging.info(f"[AUTO] Enviando mensaje: {contenido}")
        respuesta_twilio.message(contenido)
        siguiente_id = bloque_actual.get("nextId")
        if not siguiente_id:
            return None
        bloque_actual = obtener_bloque_por_id(siguiente_id)
        sesiones[sender]["current_id"] = str(bloque_actual["id"]) if bloque_actual else None
    return bloque_actual

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        sender = request.form.get('From')
        msg = request.form.get('Body', '').strip()
        logging.info(f"📩 Mensaje de {sender}: {msg}")

        respuesta = MessagingResponse()

        # Nueva sesión o reinicio con "hola"
        if sender not in sesiones or msg.lower() == "hola":
            sesiones[sender] = {
                "current_id": str(flujo[0]["id"]),
                "data": {}
            }
            logging.info("🆕 Nueva sesión iniciada")

        bloque_actual = obtener_bloque_por_id(sesiones[sender]["current_id"])
        if not bloque_actual:
            raise Exception("❌ Bloque actual no encontrado")

        tipo = bloque_actual["type"]

        # Pregunta
        if tipo == "pregunta":
            var = bloque_actual.get("variableName")
            if var:
                sesiones[sender]["data"][var] = msg
            siguiente_id = bloque_actual.get("nextId")
            bloque_actual = obtener_bloque_por_id(siguiente_id)
            sesiones[sender]["current_id"] = str(bloque_actual["id"]) if bloque_actual else None

        # Condicional
        elif tipo == "condicional":
            msg_normalizado = msg.lower()
            seleccion = None
            for i, opcion in enumerate(bloque_actual["options"], 1):
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
                opciones = "\n".join([f"{i+1}. {op['text']}" for i, op in enumerate(bloque_actual["options"])])
                respuesta.message(f"⚠️ Opción inválida. Elige una de estas:\n{opciones}")
                return str(respuesta)

        # Avanzar mensajes automáticos
        bloque_actual = avanzar_mensajes_automaticos(sender, bloque_actual, respuesta)

        # Mostrar pregunta o condicional siguiente
        if bloque_actual:
            tipo = bloque_actual["type"]
            if tipo == "pregunta":
                contenido = reemplazar_variables(bloque_actual["content"], sesiones[sender]["data"])
                respuesta.message(contenido)
            elif tipo == "condicional":
                opciones = "\n".join([f"{i+1}. {op['text']}" for i, op in enumerate(bloque_actual["options"])])
                respuesta.message(f"{bloque_actual['content']}\n{opciones}")

        return str(respuesta)

    except Exception as e:
        logging.exception("❌ Error inesperado:")
        respuesta = MessagingResponse()
        respuesta.message("⚠️ Ha ocurrido un error. Intenta nuevamente.")
        return str(respuesta)

# Config Railway
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)
