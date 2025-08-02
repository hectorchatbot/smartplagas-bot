from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import json

app = Flask(__name__)

# Cargar flujo desde archivo JSON
with open('chatbot-flujo.json', 'r', encoding='utf-8') as f:
    flujo = json.load(f)

# Diccionario para manejar sesiones (usar base de datos en producción)
sesiones = {}

def obtener_bloque_por_id(bloque_id):
    for bloque in flujo:
        if str(bloque["id"]) == str(bloque_id):
            return bloque
    return None

def reemplazar_variables(texto, data):
    for clave, valor in data.items():
        texto = texto.replace(f"{{{clave}}}", valor)
    return texto

def formatear_opciones(opciones):
    texto = ""
    for idx, opcion in enumerate(opciones, start=1):
        texto += f"{idx}. {opcion['text']}\n"
    return texto

@app.route('/webhook', methods=['POST'])
def webhook():
    sender = request.form.get('From')
    msg = request.form.get('Body').strip()
    respuesta = MessagingResponse()

    # Nueva sesión
    if msg.lower() == "hola" or sender not in sesiones:
        sesiones[sender] = {
            "current_id": str(flujo[0]["id"]),
            "data": {},
            "opciones_actuales": []
        }

    current_id = sesiones[sender]["current_id"]
    current_block = obtener_bloque_por_id(current_id)

    # Procesar entrada del usuario según tipo de bloque
    if current_block:
        tipo = current_block.get("type")

        if tipo == "pregunta":
            variable = current_block.get("variableName")
            if variable:
                sesiones[sender]["data"][variable] = msg
            next_id = current_block.get("nextId")
            sesiones[sender]["current_id"] = str(next_id)
            current_block = obtener_bloque_por_id(next_id)

        elif tipo == "condicional":
            opciones = current_block.get("options", [])
            match = None

            if msg.isdigit():
                idx = int(msg) - 1
                if 0 <= idx < len(opciones):
                    match = opciones[idx]
            if not match:
                match = next((op for op in opciones if op["text"].lower() in msg.lower()), None)

            if match:
                if "saveAs" in match:
                    sesiones[sender]["data"][match["saveAs"]] = match["text"]
                sesiones[sender]["current_id"] = str(match["nextId"])
                current_block = obtener_bloque_por_id(match["nextId"])
            else:
                respuesta.message(f"❗ Opción no válida. Escribe el número o texto exacto:\n\n{formatear_opciones(opciones)}")
                return str(respuesta)

    # Enviar bloques automáticamente
    while current_block:
        tipo = current_block.get("type")

        if tipo == "mensaje":
            contenido = reemplazar_variables(current_block["content"], sesiones[sender]["data"])
            respuesta.message(contenido)
            if current_block.get("autoAdvance", False):
                next_id = current_block.get("nextId")
                if next_id:
                    sesiones[sender]["current_id"] = str(next_id)
                    current_block = obtener_bloque_por_id(next_id)
                    continue
            break

        elif tipo == "pregunta":
            respuesta.message(current_block["content"])
            break

        elif tipo == "condicional":
            opciones = current_block.get("options", [])
            sesiones[sender]["opciones_actuales"] = opciones
            texto_opciones = formatear_opciones(opciones)
            respuesta.message(f"{current_block['content']}\n\n{texto_opciones}")
            break

        else:
            respuesta.message("Tipo de bloque no reconocido.")
            break

    return str(respuesta)
