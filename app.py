from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import json

app = Flask(__name__)

# Cargar flujo desde archivo JSON
with open('chatbot-flujo.json', 'r', encoding='utf-8') as f:
    flujo = json.load(f)

# Diccionario para manejar sesiones (reemplazar por base de datos en producción real)
sesiones = {}

# Función auxiliar para encontrar un bloque por su ID
def obtener_bloque_por_id(bloque_id):
    for bloque in flujo:
        if str(bloque["id"]) == str(bloque_id):
            return bloque
    return None

# Webhook que recibe mensajes desde Twilio WhatsApp
# Reemplaza esto en tu app.py (dentro del endpoint /webhook)

@app.route('/webhook', methods=['POST'])
def webhook():
    sender = request.form.get('From')
    msg = request.form.get('Body').strip()
    # Si el usuario escribe "hola", reinicia el flujo
    if msg.lower() == "hola":
        sesiones[sender] = {
            "current_id": str(flujo[0]["id"]),
            "data": {}
        }

    # Si es la primera vez que habla, iniciar flujo automáticamente
    if sender not in sesiones:
        sesiones[sender] = {
            "current_id": str(flujo[0]["id"]),
            "data": {}
        }

    if sender not in sesiones:
        sesiones[sender] = {
            "current_id": str(flujo[0]["id"]),  # Empieza en el primer bloque
            "data": {}
        }

    current_id = sesiones[sender]["current_id"]
    current_block = next((b for b in flujo if str(b["id"]) == str(current_id)), None)

    # (sigue la lógica para tipo mensaje, pregunta, condicional...)

    # IMPORTANTE: si current_block es tipo 'mensaje' y tiene autoAdvance, avanza automáticamente

if __name__ == '__main__':
    app.run(port=5000)
