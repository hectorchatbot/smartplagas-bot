
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import json
import logging
import os

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

with open('chatbot-flujo.json', 'r', encoding='utf-8') as f:
    flujo = json.load(f)

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
        logging.info(f"Mensaje de {sender}: {msg}")

        respuesta = MessagingResponse()

        if sender not in sesiones or msg.lower() == "hola":
            sesiones[sender] = {
                "current_id": str(flujo[0]["id"]),
                "data": {}
            }
            logging.info("Nueva sesión iniciada")

        bloque_actual = obtener_bloque_por_id(sesiones[sender]["current_id"])
        if not bloque_actual:
            raise Exception("Bloque actual no encontrado")

        tipo = bloque_actual["type"]

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
                respuesta.message(f"Opcion invalida. Elige una de estas:\n{opciones}")
                return str(respuesta)

        bloque_actual = avanzar_mensajes_automaticos(sender, bloque_actual, respuesta)

        if bloque_actual:
            tipo = bloque_actual["type"]
            if tipo == "pregunta":
                contenido = reemplazar_variables(bloque_actual["content"], sesiones[sender]["data"])
                respuesta.message(contenido)
            elif tipo == "condicional":
                opciones = "\n".join([f"{i+1}. {op['text']}" for i, op in enumerate(bloque_actual["options"])])
                respuesta.message(f"{bloque_actual['content']}\n{opciones}")
        else:
            nombre = sesiones[sender]["data"].get("nombre", "N/D")
            direccion = sesiones[sender]["data"].get("direccion", "N/D")
            comuna = sesiones[sender]["data"].get("comuna", "N/D")
            email = sesiones[sender]["data"].get("email", "N/D")
            telefono = sesiones[sender]["data"].get("telefono", "N/D")

            resumen = f"""NUEVO CLIENTE - SMART PLAGAS

Nombre: {nombre}
Direccion: {direccion}, {comuna}
Email: {email}
Telefono: {telefono}
"""

            from twilio.rest import Client
            account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
            auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
            whatsapp_from = os.environ.get("TWILIO_WHATSAPP_FROM")
            whatsapp_admin = "whatsapp:+56958166055"

            client = Client(account_sid, auth_token)
            client.messages.create(
                from_=whatsapp_from,
                to=whatsapp_admin,
                body=resumen
            )

            import smtplib
            from email.message import EmailMessage

            EMAIL_FROM = os.environ.get("EMAIL_FROM")
            EMAIL_PASS = os.environ.get("EMAIL_PASS")
            EMAIL_TO = "tucorreo@dominio.cl"

            try:
                msg = EmailMessage()
                msg["Subject"] = "Nuevo Cliente - Smart Plagas"
                msg["From"] = EMAIL_FROM
                msg["To"] = EMAIL_TO
                msg.set_content(resumen)

                with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                    smtp.login(EMAIL_FROM, EMAIL_PASS)
                    smtp.send_message(msg)
            except Exception as correo_error:
                logging.error(f"Error al enviar correo: {correo_error}")

        return str(respuesta)

    except Exception as e:
        logging.exception("Error inesperado:")
        respuesta = MessagingResponse()
        respuesta.message("Ha ocurrido un error. Intenta nuevamente.")
        return str(respuesta)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)
