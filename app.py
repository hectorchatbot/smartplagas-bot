from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import json
import logging
import os

app = Flask(__name__)

# Logging básico para Railway
logging.basicConfig(level=logging.INFO)

# Cargar flujo JSON
with open('chatbot-flujo.json', 'r', encoding='utf-8') as f:
    flujo = json.load(f)

# Sesiones en memoria (en producción real usar base de datos)
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

def avanzar_automaticamente(sender, bloque_actual, respuesta_twilio):
    while bloque_actual and bloque_actual.get("type") == "mensaje" and bloque_actual.get("autoAdvance", True):
        mensaje = reemplazar_variables(bloque_actual["content"], sesiones[sender]["data"])
        logging.info(f"[AUTO] Enviando mensaje: {mensaje}")
        respuesta_twilio.message(mensaje)
        siguiente_id = bloque_actual.get("nextId")
        if not siguiente_id:
            break
        bloque_actual = obtener_bloque_por_id(siguiente_id)
        sesiones[sender]["current_id"] = str(bloque_actual["id"]) if bloque_actual else None
    return bloque_actual

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        logging.info("🔔 Petición POST recibida en /webhook")
        sender = request.form.get('From')
        msg = request.form.get('Body', '').strip()
        logging.info(f"📩 Mensaje de {sender}: {msg}")

        respuesta = MessagingResponse()

        # Iniciar nueva sesión
        if sender not in sesiones or msg.lower() == "hola":
            sesiones[sender] = {
                "current_id": str(flujo[0]["id"]),
                "data": {}
            }
            logging.info("🆕 Nueva sesión iniciada")

        bloque_actual = obtener_bloque_por_id(sesiones[sender]["current_id"])
        if not bloque_actual or "type" not in bloque_actual:
            raise ValueError("Bloque actual inválido o sin tipo definido")

        # Procesar pregunta
        if bloque_actual["type"] == "pregunta":
            sesiones[sender]["data"][bloque_actual["variableName"]] = msg
            siguiente_id = bloque_actual.get("nextId")
            bloque_actual = obtener_bloque_por_id(siguiente_id)
            sesiones[sender]["current_id"] = str(bloque_actual["id"]) if bloque_actual else None

        # Procesar condicional con selección por número o texto
        elif bloque_actual["type"] == "condicional":
            msg_normalizado = msg.lower().strip()
            seleccion = None
            for i, op in enumerate(bloque_actual["options"], 1):
                opcion_texto = op["text"].lower()
                if msg_normalizado == str(i) or msg_normalizado in opcion_texto:
                    seleccion = op
                    break
            if seleccion:
                if "saveAs" in seleccion:
                    sesiones[sender]["data"][seleccion["saveAs"]] = seleccion["text"]
                siguiente_id = seleccion.get("nextId")
                bloque_actual = obtener_bloque_por_id(siguiente_id)
                sesiones[sender]["current_id"] = str(bloque_actual["id"]) if bloque_actual else None
            else:
                opciones = "\n".join([f"{i+1}. {op['text']}" for i, op in enumerate(bloque_actual["options"])])
                respuesta.message(f"⚠️ Opción no válida. Selecciona una de las siguientes:\n{opciones}")
                return str(respuesta)

        # Avanzar automáticamente por mensajes tipo 'mensaje'
        bloque_actual = avanzar_automaticamente(sender, bloque_actual, respuesta)

        # Mostrar pregunta o condicional
        if bloque_actual:
            if bloque_actual["type"] == "pregunta":
                respuesta.message(reemplazar_variables(bloque_actual["content"], sesiones[sender]["data"]))
            elif bloque_actual["type"] == "condicional":
                logging.info(f"➡️ Mostrando condicional: {bloque_actual['content']}")
                opciones = "\n".join([f"{i+1}. {op['text']}" for i, op in enumerate(bloque_actual["options"])])
                respuesta.message(f"{bloque_actual['content']}\n{opciones}")

        return str(respuesta)

    except Exception as e:
        logging.exception("❌ Error inesperado en /webhook:")
        respuesta = MessagingResponse()
        respuesta.message("⚠️ Ocurrió un error al procesar tu mensaje. Por favor intenta nuevamente más tarde.")
        return str(respuesta)

# Puerto y host para Railway
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)
