from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import json
import os

app = Flask(__name__)

# Cargar flujo desde archivo JSON
with open('chatbot-flujo.json', 'r', encoding='utf-8') as f:
    flujo = json.load(f)

# Diccionario para manejar sesiones (en producción reemplazar por base de datos)
sesiones = {}

# Función auxiliar para encontrar un bloque por su ID
def obtener_bloque_por_id(bloque_id):
    for bloque in flujo:
        if str(bloque["id"]) == str(bloque_id):
            return bloque
    return None

# Endpoint que recibe mensajes desde Twilio WhatsApp
@app.route('/webhook', methods=['POST'])
def webhook():
    sender = request.form.get('From')
    msg = request.form.get('Body').strip()

    # Si el usuario escribe "hola", reinicia el flujo
    if msg.lower() == "hola" or sender not in sesiones:
        sesiones[sender] = {
            "current_id": str(flujo[0]["id"]),
            "data": {}
        }

    current_id = sesiones[sender]["current_id"]
    current_block = obtener_bloque_por_id(current_id)

    respuesta = MessagingResponse()

    if current_block:
        if current_block["type"] == "mensaje":
            respuesta.message(current_block["content"])
            # Avanzar automáticamente si tiene autoAdvance
            if current_block.get("autoAdvance", False):
                next_id = current_block.get("next_id")
                if next_id:
                    sesiones[sender]["current_id"] = str(next_id)
        elif current_block["type"] == "pregunta":
            respuesta.message(current_block["content"])
            # Esperamos respuesta del usuario
        elif current_block["type"] == "condicional":
            condiciones = current_block["conditions"]
            match = next((c for c in condiciones if c["valor"].lower() in msg.lower()), None)
            if match:
                sesiones[sender]["current_id"] = str(match["next_id"])
                respuesta.message(f"Entendido: {msg}")
            else:
                respuesta.message("No entendí tu respuesta. Intenta nuevamente.")

    else:
        respuesta.message("No se encontró el siguiente paso en el flujo.")

    return str(respuesta)

# Lanzar la app (Railway, Render, local)
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
