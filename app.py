from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import json

app = Flask(__name__)

# Cargar flujo desde archivo JSON
with open('chatbot-flujo.json', 'r', encoding='utf-8') as f:
    flujo = json.load(f)

# Diccionario para manejar sesiones (usar base de datos en producción)
sesiones = {}

# Función para obtener un bloque por ID
def obtener_bloque_por_id(bloque_id):
    for bloque in flujo:
        if str(bloque["id"]) == str(bloque_id):
            return bloque
    return None

# Reemplazar variables como {nombre}, {email}, etc.
def reemplazar_variables(texto, data):
    for clave, valor in data.items():
        texto = texto.replace(f"{{{clave}}}", valor)
    return texto

# Ruta webhook para Twilio
@app.route('/webhook', methods=['POST'])
def webhook():
    sender = request.form.get('From')
    msg = request.form.get('Body').strip()
    respuesta = MessagingResponse()

    # Iniciar sesión si es nuevo o dice "hola"
    if msg.lower() == "hola" or sender not in sesiones:
        sesiones[sender] = {
            "current_id": str(flujo[0]["id"]),
            "data": {}
        }

        current_id = sesiones[sender]["current_id"]
        current_block = obtener_bloque_por_id(current_id)

        while current_block:
            tipo = current_block.get("type")

            if tipo == "mensaje":
                content = reemplazar_variables(current_block["content"], sesiones[sender]["data"])
                respuesta.message(content)
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
                texto_opciones = "\n".join([op["text"] for op in opciones])
                respuesta.message(f"{current_block['content']}\n\n{texto_opciones}")
                break

            else:
                respuesta.message("Tipo de bloque no reconocido.")
                break

        return str(respuesta)

    # Continuar flujo si ya estaba en sesión
    current_id = sesiones[sender]["current_id"]
    current_block = obtener_bloque_por_id(current_id)

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
            match = next((op for op in opciones if op["text"].lower() in msg.lower()), None)
            if match:
                sesiones[sender]["current_id"] = str(match["nextId"])
                current_block = obtener_bloque_por_id(match["nextId"])
            else:
                respuesta.message("❗ No entendí tu respuesta. Por favor elige una opción válida del menú.")
                return str(respuesta)

    # Ejecutar el siguiente bloque
    while current_block:
        tipo = current_block.get("type")

        if tipo == "mensaje":
            content = reemplazar_variables(current_block["content"], sesiones[sender]["data"])
            respuesta.message(content)
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
            texto_opciones = "\n".join([op["text"] for op in opciones])
            respuesta.message(f"{current_block['content']}\n\n{texto_opciones}")
            break

        else:
            respuesta.message("Tipo de bloque no reconocido.")
            break

    return str(respuesta)
