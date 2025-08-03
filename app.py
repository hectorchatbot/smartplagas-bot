from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import json

app = Flask(__name__)

# Cargar flujo desde archivo
with open('chatbot-flujo.json', 'r', encoding='utf-8') as f:
    flujo = json.load(f)

# Sesiones temporales (usa base de datos en producción)
sesiones = {}

# Buscar bloque por ID
def obtener_bloque(bloque_id):
    for bloque in flujo:
        if bloque['id'] == bloque_id:
            return bloque
    return None

# Procesar siguiente bloque automáticamente
def avanzar_flujo(sender, respuesta=None):
    sesion = sesiones[sender]
    bloque_actual = obtener_bloque(sesion["current_id"])

    # Reemplazo de campos
    nombre_usuario = sesion["data"].get("nombre", "")
    
    # Manejo por tipo de bloque
    if bloque_actual["tipo"] == "mensaje":
        contenido = bloque_actual["contenido"].replace("{{nombre}}", nombre_usuario)
        siguiente_bloque = bloque_actual.get("siguiente")
        if siguiente_bloque:
            sesion["current_id"] = siguiente_bloque
            return [contenido] + avanzar_flujo(sender)
        else:
            return [contenido]

    elif bloque_actual["tipo"] == "pregunta":
        if "campo" in bloque_actual:
            if respuesta:
                sesion["data"][bloque_actual["campo"]] = respuesta
                sesion["current_id"] = bloque_actual["siguiente"]
                return avanzar_flujo(sender)
            else:
                return [bloque_actual["contenido"]]
        elif "opciones" in bloque_actual:
            if respuesta in bloque_actual["opciones"]:
                sesion["current_id"] = bloque_actual["opciones"][respuesta]
                return avanzar_flujo(sender)
            else:
                return ["Por favor, elige una opción válida:\n" + bloque_actual["contenido"]]
        else:
            return ["Tipo de bloque no reconocido."]
    else:
        return ["Tipo de bloque no reconocido."]

@app.route("/webhook", methods=['POST'])
def webhook():
    sender = request.form.get("From")
    msg = request.form.get("Body").strip()
    respuesta = MessagingResponse()

    if sender not in sesiones or msg.lower() in ["hola", "empezar"]:
        sesiones[sender] = {"current_id": "inicio", "data": {}}
        mensajes = avanzar_flujo(sender)
    else:
        mensajes = avanzar_flujo(sender, msg)

    for mensaje in mensajes:
        respuesta.message(mensaje)

    return str(respuesta)
