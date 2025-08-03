from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import json

app = Flask(__name__)

# Cargar flujo desde archivo JSON
with open('chatbot-flujo.json', 'r', encoding='utf-8') as f:
    flujo = json.load(f)

# Diccionario para manejar sesiones (en producción usar BD)
sesiones = {}

# Función para encontrar un bloque por ID
def obtener_bloque(bloque_id):
    for bloque in flujo:
        if bloque["id"] == bloque_id:
            return bloque
    return None

# Función para reemplazar variables como {{nombre}}
def reemplazar_variables(texto, datos):
    for clave, valor in datos.items():
        texto = texto.replace(f"{{{{{clave}}}}}", valor)
    return texto

# Enviar automáticamente bloques tipo 'mensaje'
def avanzar_automaticamente(sid, resp):
    while True:
        bloque = obtener_bloque(sesiones[sid]["current_id"])
        if bloque["tipo"] == "mensaje":
            contenido = reemplazar_variables(bloque["contenido"], sesiones[sid]["data"])
            resp.message(contenido)
            if "siguiente" in bloque:
                sesiones[sid]["current_id"] = bloque["siguiente"]
            else:
                break
        else:
            break

@app.route('/webhook', methods=['POST'])
def webhook():
    sender = request.form.get('From')
    msg = request.form.get('Body').strip()
    resp = MessagingResponse()

    if sender not in sesiones or msg.lower() == "hola":
        sesiones[sender] = {
            "current_id": "inicio",
            "data": {}
        }

    avanzar_automaticamente(sender, resp)
    bloque_actual = obtener_bloque(sesiones[sender]["current_id"])

    if bloque_actual["tipo"] == "pregunta":
        # Si viene de un bloque con campo para guardar datos
        if "campo" in bloque_actual:
            sesiones[sender]["data"][bloque_actual["campo"]] = msg
            if "siguiente" in bloque_actual:
                sesiones[sender]["current_id"] = bloque_actual["siguiente"]
                avanzar_automaticamente(sender, resp)
            else:
                siguiente = flujo[flujo.index(bloque_actual) + 1]["id"]
                sesiones[sender]["current_id"] = siguiente
                avanzar_automaticamente(sender, resp)

        # Si es una pregunta con opciones
        elif "opciones" in bloque_actual:
            if msg in bloque_actual["opciones"]:
                sesiones[sender]["current_id"] = bloque_actual["opciones"][msg]
                avanzar_automaticamente(sender, resp)
            else:
                opciones = bloque_actual["contenido"]
                resp.message(f"Opción no válida. Por favor elige una opción:\n\n{opciones}")
        else:
            resp.message("Error: bloque 'pregunta' sin 'campo' ni 'opciones'.")

    elif bloque_actual["tipo"] == "mensaje":
        avanzar_automaticamente(sender, resp)

    else:
        resp.message("Tipo de bloque no reconocido.")

    return str(resp)
