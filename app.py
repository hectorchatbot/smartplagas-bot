from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import json

app = Flask(__name__)

# Cargar el flujo desde archivo JSON
with open('chatbot-flujo.json', 'r', encoding='utf-8') as f:
    flujo = json.load(f)

# Simulación de sesiones (en producción usar DB)
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
    while bloque_actual and bloque_actual["type"] == "mensaje" and bloque_actual.get("autoAdvance"):
        mensaje = reemplazar_variables(bloque_actual["content"], sesiones[sender]["data"])
        respuesta_twilio.message(mensaje)
        siguiente_id = bloque_actual.get("nextId")
        if not siguiente_id:
            break
        bloque_actual = obtener_bloque_por_id(siguiente_id)
        sesiones[sender]["current_id"] = str(bloque_actual["id"]) if bloque_actual else None
    return bloque_actual

@app.route('/webhook', methods=['POST'])
def webhook():
    sender = request.form.get('From')
    msg = request.form.get('Body', '').strip()

    respuesta = MessagingResponse()

    if sender not in sesiones or msg.lower() == "hola":
        sesiones[sender] = {
            "current_id": str(flujo[0]["id"]),
            "data": {}
        }

    bloque_actual = obtener_bloque_por_id(sesiones[sender]["current_id"])

    # Procesar entrada si es pregunta o condicional
    if bloque_actual["type"] == "pregunta":
        sesiones[sender]["data"][bloque_actual["variableName"]] = msg
        siguiente_id = bloque_actual.get("nextId")
        bloque_actual = obtener_bloque_por_id(siguiente_id)
        sesiones[sender]["current_id"] = str(bloque_actual["id"]) if bloque_actual else None

    elif bloque_actual["type"] == "condicional":
        seleccion = next((op for op in bloque_actual["options"] if op["text"].lower() == msg.lower()), None)
        if seleccion:
            if "saveAs" in seleccion:
                sesiones[sender]["data"][seleccion["saveAs"]] = seleccion["text"]
            siguiente_id = seleccion["nextId"]
            bloque_actual = obtener_bloque_por_id(siguiente_id)
            sesiones[sender]["current_id"] = str(bloque_actual["id"]) if bloque_actual else None
        else:
            opciones = "\n".join([op["text"] for op in bloque_actual["options"]])
            respuesta.message(f"Por favor, selecciona una opción válida:\n{opciones}")
            return str(respuesta)

    # Avanzar automáticamente por mensajes
    bloque_actual = avanzar_automaticamente(sender, bloque_actual, respuesta)

    # Mostrar pregunta o condicional
    if bloque_actual:
        if bloque_actual["type"] == "pregunta":
            respuesta.message(reemplazar_variables(bloque_actual["content"], sesiones[sender]["data"]))
        elif bloque_actual["type"] == "condicional":
            opciones = "\n".join([op["text"] for op in bloque_actual["options"]])
            respuesta.message(f"{bloque_actual['content']}\n{opciones}")

    return str(respuesta)

if __name__ == '__main__':
    app.run(debug=True)
